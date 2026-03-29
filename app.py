import os
import logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from werkzeug.utils import secure_filename
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure, ServerSelectionTimeoutError
from bson.objectid import ObjectId
from dotenv import load_dotenv
import bcrypt
from datetime import datetime
from groq import Groq

load_dotenv()
app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'fallback_secret')

# --- MongoDB: explicit timeouts so failures surface quickly ---
MONGO_URI = os.getenv('MONGO_URI', 'mongodb://localhost:27017/')
mongo_ok = False
client = None
db = None
users_col = None
reviews_col = None

try:
    client = MongoClient(
        MONGO_URI,
        serverSelectionTimeoutMS=8000,
        connectTimeoutMS=8000,
    )
    client.admin.command("ping")
    db = client.sciencehub
    users_col = db.users
    reviews_col = db.reviews
    mongo_ok = True
except (ConnectionFailure, ServerSelectionTimeoutError, Exception) as e:
    print("MongoDB connection failed:", e)
    mongo_ok = False

# --- AI: Groq Configuration ---
groq_key = os.getenv("API_KEY") or os.getenv("GROQ_API_KEY")
if groq_key and groq_key.startswith("gsk_"):
    ai_client = Groq(api_key=groq_key)
else:
    ai_client = None

PROFESSIONS = ("Student", "Doctor", "Patient", "Researcher", "Educator", "Other")

UPLOAD_DIR = os.path.join("static", "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)


def review_author_role(user_doc):
    """Map stored profession/legacy role to Doctor vs Patient for review filters."""
    if not user_doc:
        return "Patient"
    prof = (user_doc.get("profession") or user_doc.get("role") or "Patient").strip()
    if prof == "Doctor":
        return "Doctor"
    return "Patient"


def generate_ai_response(query, category, sub_category):
    """Try Grok AI."""
    system_medical = (
        "You are a helpful medical education assistant. "
        "Give clear, concise general information—not a diagnosis. "
        "Remind the user to see a qualified clinician for personal medical decisions."
    )
    user_prompt = f"Category: {category or 'general'}. "
    if sub_category:
        user_prompt += f"Sub-area: {sub_category}. "
    user_prompt += f"Question: {query}"

    if ai_client:
        try:
            chat_completion = ai_client.chat.completions.create(
                messages=[
                    {"role": "system", "content": system_medical},
                    {"role": "user", "content": user_prompt},
                ],
                model="llama-3.3-70b-versatile",
                max_tokens=400,
            )
            text = chat_completion.choices[0].message.content
            if text and text.strip():
                return text.strip()
        except Exception as e:
            logging.error("Groq API error: %s", e)

    if ai_client is None:
        return (
            "AI is not configured. Add your API_KEY (starting with gsk_) to your .env file."
        )
    return (
        "The AI service is temporarily unavailable. "
        "You can still read community insights below."
    )


def get_current_user():
    if not mongo_ok or users_col is None:
        return None
    if "user_id" not in session:
        return None
    try:
        return users_col.find_one({"_id": ObjectId(session["user_id"])})
    except Exception:
        return None


# ================= ROUTES =================


@app.route("/")
@app.route("/index.html")
def index():
    user = get_current_user()
    return render_template("index.html", user=user)


@app.route("/assistant")
@app.route("/lp.html")
def assistant():
    user = get_current_user()
    return render_template("lp.html", user=user)


@app.route("/login")
@app.route("/login.html")
def login_page():
    if get_current_user():
        return redirect(url_for("index"))
    return render_template("login.html")


@app.route("/register")
@app.route("/register.html")
def register_page():
    if get_current_user():
        return redirect(url_for("index"))
    return render_template("register.html")


@app.route("/api/health")
def health():
    return jsonify({"ok": True, "mongodb": mongo_ok})


# ================= AUTH API =================


@app.route("/api/signup", methods=["POST"])
def signup():
    if not mongo_ok:
        return jsonify({"error": "Database unavailable. Start MongoDB or check MONGO_URI in .env."}), 503

    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    age = data.get("age")

    if not email or not password:
        return jsonify({"error": "Email and password are required"}), 400

    if users_col.find_one({"email": email}):
        return jsonify({"error": "Email already exists"}), 400

    hashed = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt())

    doc = {
        "name": name,
        "email": email,
        "password": hashed,
        "profession": "",
        "age": age,
    }
    user_id = users_col.insert_one(doc).inserted_id

    session["user_id"] = str(user_id)
    session["user_name"] = name
    session["user_role"] = "Patient"

    return jsonify({"success": True, "message": "Account created successfully"})


@app.route("/api/login", methods=["POST"])
def login():
    if not mongo_ok:
        return jsonify({"error": "Database unavailable. Start MongoDB or check MONGO_URI in .env."}), 503

    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    if not email or not password:
        return jsonify({"error": "Email and password are required"}), 400

    user = users_col.find_one({"email": email})
    if user:
        stored_pw = user.get("password")
        if not stored_pw:
            return jsonify({"error": "Invalid credentials"}), 401
            
        if isinstance(stored_pw, str):
            stored_pw = stored_pw.encode("utf-8")
            
        try:
            if bcrypt.checkpw(password.encode("utf-8"), stored_pw):
                session["user_id"] = str(user["_id"])
                session["user_name"] = user.get("name", "User")
                session["user_role"] = review_author_role(user)
                return jsonify({"success": True, "message": "Logged in successfully"})
        except ValueError as e:
            logging.error("Bcrypt validation failed for user %s: %s", email, e)
            return jsonify({"error": "Invalid credentials or legacy account."}), 401

    return jsonify({"error": "Invalid credentials"}), 401


@app.route("/api/logout")
@app.route("/logout.html")
def logout():
    session.clear()
    return redirect(url_for("index"))


# ================= UI HELPER APIS =================


def _profession_for_api(user):
    if not user:
        return ""
    prof = (user.get("profession") or "").strip()
    if prof:
        return prof if prof in PROFESSIONS else "Other"
    legacy = (user.get("role") or "").strip()
    if legacy in ("Doctor", "Patient"):
        return legacy
    return ""


@app.route("/api/me")
def me():
    user = get_current_user()
    if not user:
        return jsonify({"logged_in": False})

    prof = _profession_for_api(user)

    return jsonify(
        {
            "logged_in": True,
            "name": user.get("name", ""),
            "profession": prof,
            "role": review_author_role(user),
            "profile_pic": user.get("profile_pic"),
        }
    )


# ================= PROFILE UI =================


@app.route("/profile")
@app.route("/profile.html")
def profile_page():
    user = get_current_user()
    if not user:
        return redirect(url_for("login_page"))
    return render_template("profile.html", user=user)


@app.route("/api/profile_data", methods=["GET"])
def profile_data():
    user = get_current_user()
    if not user:
        return jsonify({"error": "You must be logged in"}), 401

    prof = _profession_for_api(user)

    return jsonify(
        {
            "name": user.get("name", ""),
            "email": user.get("email", ""),
            "dob": user.get("dob", ""),
            "gender": user.get("gender", ""),
            "country": user.get("country", ""),
            "bio": user.get("bio", ""),
            "profession": prof,
            "profile_pic": user.get("profile_pic"),
        }
    )


@app.route("/api/profile", methods=["POST"])
def profile_update():
    user = get_current_user()
    if not user:
        return jsonify({"error": "You must be logged in"}), 401

    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    dob = data.get("dob", "")
    gender = data.get("gender", "")
    country = (data.get("country") or "").strip()
    bio = (data.get("bio") or "").strip()
    profession = (data.get("profession") or "").strip()

    if not name:
        return jsonify({"error": "Name is required"}), 400

    if profession and profession not in PROFESSIONS:
        return jsonify({"error": "Invalid profession"}), 400

    users_col.update_one(
        {"_id": ObjectId(session["user_id"])},
        {"$set": {"name": name, "dob": dob, "gender": gender, "country": country, "bio": bio, "profession": profession}},
    )
    session["user_name"] = name
    session["user_role"] = review_author_role({"profession": profession})
    return jsonify({"success": True})


@app.route("/api/profile/picture", methods=["POST"])
def profile_picture_upload():
    user = get_current_user()
    if not user:
        return jsonify({"error": "You must be logged in"}), 401

    if "file" not in request.files:
        return jsonify({"error": "Missing file upload"}), 400
    file = request.files["file"]
    if not file or not file.filename:
        return jsonify({"error": "Missing file upload"}), 400

    filename = secure_filename(file.filename)
    ext = os.path.splitext(filename)[1].lower()
    allowed_ext = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
    if ext not in allowed_ext:
        return jsonify({"error": "Unsupported file type"}), 400

    new_filename = f"{session['user_id']}_profile{ext}"
    save_path = os.path.join(UPLOAD_DIR, new_filename)
    file.save(save_path)

    profile_pic_url = f"/static/uploads/{new_filename}"
    users_col.update_one({"_id": ObjectId(session["user_id"])}, {"$set": {"profile_pic": profile_pic_url}})
    return jsonify({"success": True, "profile_pic": profile_pic_url})


# ================= AI & REVIEWS API =================


@app.route("/api/search", methods=["POST"])
def search_ai():
    import re

    data = request.get_json(silent=True) or {}
    query = data.get("query")
    category = data.get("category") or ""
    sub_category = data.get("subCategory") or ""
    page = int(data.get("page") or 0)

    if not query:
        return jsonify({"error": "Missing query"}), 400

    try:
        if not mongo_ok or reviews_col is None:
            ai_answer = ""
            if page == 0:
                ai_answer = generate_ai_response(query, category, sub_category)
            return jsonify(
                {
                    "success": True,
                    "ai_answer": ai_answer if page == 0 else "",
                    "reviews": [],
                    "has_more": False,
                }
            )

        ai_answer = ""
        if page == 0:
            ai_answer = generate_ai_response(query, category, sub_category)

        stopwords = {
            "how",
            "to",
            "what",
            "is",
            "a",
            "the",
            "my",
            "are",
            "in",
            "of",
            "and",
            "i",
            "have",
            "for",
            "with",
            "can",
            "do",
            "it",
            "on",
            "this",
            "that",
            "get",
            "an",
            "or",
        }
        words = [w for w in re.findall(r"\w+", query.lower()) if w not in stopwords and len(w) > 3]
        regex_pattern = "|".join(words) if words else ""

        cat_clause = {"category": category} if category else {}

        if regex_pattern:
            text_clause = {
                "$or": [
                    {"content": {"$regex": regex_pattern, "$options": "i"}},
                    {"Review": {"$regex": regex_pattern, "$options": "i"}},
                ]
            }
            if category:
                base_filter = {"$and": [cat_clause, text_clause]}
            else:
                base_filter = text_clause
        else:
            base_filter = dict(cat_clause) if category else {}

        serialized_reviews = []
        role_patient = {
            "$or": [{"author_role": "Patient"}, {"author_role": {"$exists": False}}],
        }
        if page == 0:
            doctor_filter = dict(base_filter)
            doctor_filter["author_role"] = "Doctor"
            doctor_reviews = list(reviews_col.find(doctor_filter).sort("_id", -1).limit(2))

            if not base_filter:
                patient_filter = role_patient
            else:
                patient_filter = {"$and": [base_filter, role_patient]}

            patient_reviews = list(reviews_col.find(patient_filter).sort("_id", -1).limit(8))
            combined_reviews = doctor_reviews + patient_reviews
        else:
            skip_amount = 10 + (page - 1) * 10
            combined_reviews = list(reviews_col.find(base_filter).sort("_id", -1).skip(skip_amount).limit(10))

        for r in combined_reviews:
            r["_id"] = str(r["_id"])
            created_at = r.get("createdAt")
            if isinstance(created_at, datetime):
                r["createdAt"] = created_at.isoformat()
            else:
                r["createdAt"] = created_at if created_at else datetime.utcnow().isoformat()
            r["content"] = r.get("content") or r.get("Review", "No content provided.")
            r["author_name"] = r.get("author_name") or r.get("Name", "Anonymous")
            r["author_role"] = r.get("author_role", "Patient")
            serialized_reviews.append(r)

        return jsonify(
            {
                "success": True,
                "ai_answer": ai_answer if page == 0 else "",
                "reviews": serialized_reviews,
                "has_more": len(combined_reviews) > 0 and len(combined_reviews) >= (10 if page > 0 else 0),
            }
        )

    except Exception as e:
        print("Search API Error:", e)
        return jsonify({"error": "Failed to process search request."}), 500


@app.route("/api/reviews", methods=["POST"])
def post_review():
    if not mongo_ok:
        return jsonify({"error": "Database unavailable."}), 503
    if "user_id" not in session:
        return jsonify({"error": "You must be logged in to post a review."}), 401

    data = request.get_json(silent=True) or {}
    category = data.get("category")
    content = data.get("content")
    sub_category = data.get("subCategory") or ""
    related_query = data.get("relatedQuery") or sub_category or ""

    if not category or not content:
        return jsonify({"error": "Missing category or content"}), 400

    try:
        u = get_current_user()
        review_doc = {
            "user_id": session["user_id"],
            "author_name": session.get("user_name", "Anonymous"),
            "author_role": review_author_role(u),
            "category": category,
            "content": content,
            "subCategory": sub_category,
            "related_query": related_query,
            "createdAt": datetime.utcnow(),
        }

        insert_res = reviews_col.insert_one(review_doc)
        return jsonify({"success": True, "review_id": str(insert_res.inserted_id)})

    except Exception as e:
        print("Review POST error:", e)
        return jsonify({"error": "Failed to post review"}), 500


if __name__ == "__main__":
    import webbrowser
    from threading import Timer

    def open_browser():
        if not os.environ.get("WERKZEUG_RUN_MAIN"):
            webbrowser.open_new("http://localhost:5000/")

    Timer(1.5, open_browser).start()
    app.run(debug=True)
