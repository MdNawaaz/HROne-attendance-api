import os
from datetime import datetime, date, timedelta, timezone
from typing import Optional
from fastapi import FastAPI, HTTPException, Query, status
from pydantic import BaseModel, Field
from pymongo import ASCENDING, DESCENDING, MongoClient
from dotenv import load_dotenv

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

@app.on_event("startup")
def configure_indexes():
    db.employees.create_index([("emp_code", ASCENDING)], unique=True)
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", DESCENDING)], unique=True)
    db.attendance_logs.create_index([("date", ASCENDING)])
    db.employees.create_index([("department", ASCENDING)])

# --------------------------------------------------------------------------- #
# Helpers & Business Rules (R1 - R5)
# --------------------------------------------------------------------------- #
def compute_late_minutes(punch_in: datetime, shift_start: str) -> int:
    h, m = map(int, shift_start.split(":"))
    start = punch_in.astimezone(IST).replace(hour=h, minute=m, second=0, microsecond=0)
    diff_minutes = int((punch_in - start).total_seconds() / 60)
    return diff_minutes if diff_minutes > 10 else 0

def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = (punch_out - punch_in).total_seconds()
    return round((seconds / 3600.0) + 1e-9, 2)

def compute_overtime(punch_out: datetime, shift_end: str, date_str: str) -> int:
    h, m = map(int, shift_end.split(":"))
    shift_end_dt = datetime.fromisoformat(date_str).replace(hour=h, minute=m, tzinfo=IST)
    overtime_mins = int((punch_out - shift_end_dt).total_seconds() / 60)
    return overtime_mins if overtime_mins >= 30 else 0

def compute_half_day(work_hours: float) -> bool:
    return work_hours < 4.50

# --------------------------------------------------------------------------- #
# Pydantic Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

class PatchAttendanceIn(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str
    by: str = "hr.admin"

# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unhealthy")

@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if db.employees.find_one({"emp_code": body.emp_code}):
        raise HTTPException(409, "emp_code already exists")
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc)
    db.employees.insert_one(doc)
    doc.pop("_id", None)
    return doc

@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = 1, page_size: int = 20):
    q = {}
    if department:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    items = list(db.employees.find(q, {"_id": 0}).skip(skip).limit(page_size))
    return {"items": items, "total": total, "page": page, "page_size": page_size}

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")
    
    ts = datetime.fromtimestamp(body.punched_at / 1000, tz=IST) if body.punched_at else datetime.now(IST)
    d = ts.date().isoformat()
    
    if db.attendance_logs.find_one({"emp_code": body.emp_code, "date": d}):
        raise HTTPException(409, "already punched in for this date")
        
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    db.attendance_logs.insert_one(doc)
    doc["id"] = str(doc.pop("_id"))
    return doc

@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "Employee not found")
        
    ts = datetime.fromtimestamp(body.punched_at / 1000, tz=IST) if body.punched_at else datetime.now(IST)
    d = ts.date().isoformat()
    
    record = db.attendance_logs.find_one({"emp_code": body.emp_code, "date": d})
    if not record or not record.get("punch_in"):
        raise HTTPException(400, "No active punch-in found for today")
        
    if record.get("punch_out"):
        raise HTTPException(400, "Already punched out")
        
    p_in = record["punch_in"]
    wh = compute_work_hours(p_in, ts)
    ot = compute_overtime(ts, emp["shift_end"], d)
    hd = compute_half_day(wh)
    
    db.attendance_logs.update_one(
        {"_id": record["_id"]},
        {"$set": {"punch_out": ts, "work_hours": wh, "overtime_minutes": ot, "half_day": hd}}
    )
    
    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    updated["id"] = str(updated.pop("_id"))
    return updated

@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
):
    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
        
    skip = (page - 1) * page_size
    total = db.attendance_logs.count_documents(q)
    docs = list(db.attendance_logs.find(q).sort("date", DESCENDING).skip(skip).limit(page_size))
    for d in docs:
        d["id"] = str(d.pop("_id"))
    return {"items": docs, "total": total, "page": page, "page_size": page_size}

@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: PatchAttendanceIn):
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(404, "Attendance record not found")
        
    emp = db.employees.find_one({"emp_code": emp_code})
    changes = {}
    update_data = {}
    
    if body.status and body.status != record.get("status"):
        changes["status"] = {"from": record.get("status"), "to": body.status}
        update_data["status"] = body.status
        
    if body.punch_in is not None:
        new_pin = datetime.fromtimestamp(body.punch_in / 1000, tz=IST)
        old_pin = record.get("punch_in")
        if new_pin != old_pin:
            changes["punch_in"] = {"from": old_pin, "to": new_pin}
            update_data["punch_in"] = new_pin
            new_late = compute_late_minutes(new_pin, emp["shift_start"])
            if new_late != record.get("late_minutes", 0):
                changes["late_minutes"] = {"from": record.get("late_minutes", 0), "to": new_late}
                update_data["late_minutes"] = new_late

    if body.punch_out is not None:
        new_pout = datetime.fromtimestamp(body.punch_out / 1000, tz=IST)
        old_pout = record.get("punch_out")
        if new_pout != old_pout:
            changes["punch_out"] = {"from": old_pout, "to": new_pout}
            update_data["punch_out"] = new_pout
            
    current_pin = update_data.get("punch_in", record.get("punch_in"))
    current_pout = update_data.get("punch_out", record.get("punch_out"))
    if current_pin and current_pout:
        new_wh = compute_work_hours(current_pin, current_pout)
        if new_wh != record.get("work_hours"):
            changes["work_hours"] = {"from": record.get("work_hours"), "to": new_wh}
            update_data["work_hours"] = new_wh
        new_ot = compute_overtime(current_pout, emp["shift_end"], date)
        if new_ot != record.get("overtime_minutes", 0):
            changes["overtime_minutes"] = {"from": record.get("overtime_minutes", 0), "to": new_ot}
            update_data["overtime_minutes"] = new_ot
        new_hd = compute_half_day(new_wh)
        if new_hd != record.get("half_day", False):
            changes["half_day"] = {"from": record.get("half_day", False), "to": new_hd}
            update_data["half_day"] = new_hd

    if not changes:
        return record

    history_entry = {
        "at": datetime.now(timezone.utc),
        "by": body.by,
        "reason": body.reason,
        "changes": changes
    }
    
    db.attendance_logs.update_one(
        {"_id": record["_id"]},
        {
            "$set": update_data,
            "$push": {"history": history_entry}
        }
    )
    
    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    updated["id"] = str(updated.pop("_id"))
    return updated

# --------------------------------------------------------------------------- #
# Full Analytics & Explain Endpoints
# --------------------------------------------------------------------------- #

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$regex": f"^{month}"}}},
        {
            "$group": {
                "_id": "$emp_code",
                "total_logs": {"$sum": 1},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0
                        ]
                    }
                },
                "total_work_hours": {"$sum": {"$ifNull": ["$work_hours", 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        }
    ]
    res = list(db.attendance_logs.aggregate(pipeline))
    if not res:
        return {
            "emp_code": emp_code, "month": month,
            "total_logs": 0, "present_days": 0.0,
            "total_work_hours": 0.0, "total_late_minutes": 0,
            "total_overtime_minutes": 0
        }
    doc = res[0]
    doc.pop("_id", None)
    doc["emp_code"] = emp_code
    doc["month"] = month
    doc["total_work_hours"] = round(doc["total_work_hours"], 2)
    return doc

@app.get("/analytics/departments/summary")
def department_summary(department: str, month: str):
    # Headcount as per R9 and cross-collection aggregation
    pipeline = [
        {"$match": {"department": department, "joined_on": {"$lte": f"{month}-31"}}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"ec": "$emp_code"},
                "pipeline": [
                    {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$ec"]}, {"$regexMatch": {"input": "$date", "regex": f"^{month}"}}]}}}
                ],
                "as": "logs"
            }
        },
        {
            "$project": {
                "emp_code": 1,
                "logs_count": {"$size": "$logs"},
                "work_hours": {"$sum": "$logs.work_hours"}
            }
        },
        {
            "$group": {
                "_id": None,
                "headcount": {"$sum": 1},
                "total_work_hours": {"$sum": "$work_hours"},
                "avg_work_hours": {"$avg": "$work_hours"}
            }
        }
    ]
    res = list(db.employees.aggregate(pipeline))
    if not res:
        return {"department": department, "month": month, "headcount": 0, "total_work_hours": 0.0, "avg_work_hours": 0.0}
    doc = res[0]
    doc.pop("_id", None)
    doc["department"] = department
    doc["month"] = month
    doc["total_work_hours"] = round(doc.get("total_work_hours", 0.0), 2)
    doc["avg_work_hours"] = round(doc.get("avg_work_hours", 0.0), 2)
    return doc

@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = 10):
    pipeline = [
        {"$match": {"date": {"$regex": f"^{month}"}}},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}}
            }
        },
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$limit": limit}
    ]
    items = list(db.attendance_logs.aggregate(pipeline))
    result = []
    prev_late = None
    prev_rank = 1
    for idx, item in enumerate(items, start=1):
        late_mins = item["total_late_minutes"]
        if late_mins == prev_late:
            rank = prev_rank
        else:
            rank = idx
            prev_rank = rank
            prev_late = late_mins
            
        result.append({
            "rank": rank,
            "emp_code": item["_id"],
            "total_late_minutes": late_mins,
            "late_count": item["late_count"]
        })
    return {"month": month, "leaderboard": result}

@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, date_from: str, date_to: str):
    # Daily trend with aggregation
    pipeline = [
        {"$match": {"date": {"$gte": date_from, "$lte": date_to}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp"
            }
        },
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {
            "$group": {
                "_id": "$date",
                "present_count": {"$sum": {"$cond": [{"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}, 1, 0]}},
                "total_work_hours": {"$sum": {"$ifNull": ["$work_hours", 0]}}
            }
        },
        {"$sort": {"_id": ASCENDING}}
    ]
    trend_data = list(db.attendance_logs.aggregate(pipeline))
    formatted_trend = []
    for item in trend_data:
        formatted_trend.append({
            "date": item["_id"],
            "present_count": item["present_count"],
            "total_work_hours": round(item["total_work_hours"], 2)
        })
    return {"department": department, "date_from": date_from, "date_to": date_to, "trend": formatted_trend}

@app.get("/admin/explain/{endpoint}")
def admin_explain(endpoint: str):
    if endpoint == "attendance_list":
        return db.attendance_logs.find().explain("executionStats")
    elif endpoint == "employee_monthly":
        return db.attendance_logs.find().explain("executionStats")
    elif endpoint == "department_summary":
        return db.employees.find().explain("executionStats")
    elif endpoint == "late_leaderboard":
        return db.attendance_logs.find().explain("executionStats")
    elif endpoint == "department_trend":
        return db.attendance_logs.find().explain("executionStats")
    raise HTTPException(404, "Invalid endpoint name for explain")