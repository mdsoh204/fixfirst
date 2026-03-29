import json
from pymongo import MongoClient
import math
from datetime import datetime, timezone

client = MongoClient("mongodb://localhost:27017/")
db = client["sciencehub"]
collection = db["reviews"]

print("Deleting old imports...")
collection.delete_many({'user_id': 'system_import'})

with open("data.json", encoding="utf-8") as f:
    data = json.load(f)

mapped_data = []
for item in data:
    review_text = item.get("Review")
    if not isinstance(review_text, str) or not review_text.strip():
        continue
    
    age = item.get("Age")
    try:
        author_age = str(int(float(age))) if age is not None and not math.isnan(float(age)) else "Unknown"
    except Exception:
        author_age = "Unknown"
        
    review_doc = {
        'user_id': 'system_import',
        'author_name': item.get("Name", "Anonymous"),
        'author_age': author_age,
        'author_gender': item.get("Gender", "Unknown"),
        'blood_pressure': item.get("Blood Pressure", 0),
        'stress_level': item.get("Stress Level", "Unknown"),
        'bmi': item.get("BMI", 0),
        'cholesterol': item.get("Cholesterol Level", 0),
        'heart_disease_status': item.get("Heart Disease Status", "Unknown"),
        'category': 'medical',
        'content': review_text.strip(),
        'subCategory': 'Heart / Cardiology',
        'related_query': 'Heart / Cardiology',
        'createdAt': datetime.now(timezone.utc)
    }
    mapped_data.append(review_doc)

if mapped_data:
    collection.insert_many(mapped_data)
    print(f"Data mapped and {len(mapped_data)} records inserted successfully into sciencehub.reviews!")
else:
    print("No valid records found to insert.")