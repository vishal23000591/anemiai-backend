from flask import Flask, request, jsonify
import tensorflow as tf
import numpy as np
import pickle
from flask_cors import CORS
from pymongo import MongoClient
import bcrypt
from ultralytics import YOLO
import cv2
import base64
from io import BytesIO
from PIL import Image
import requests  # <-- for chatbot API

app = Flask(__name__)
CORS(app)

# -------------------- DATABASE --------------------
client = MongoClient("mongodb+srv://vishalsuresh_db:vishal1234@anemiaicluster.kgtsjjn.mongodb.net/?retryWrites=true&w=majority")
db = client["vishalsuresh_db"]
users_collection = db["users"]

# -------------------- ML MODELS --------------------
model = tf.keras.models.load_model("model/anemia_model.h5", compile=False)
scaler = pickle.load(open("model/scaler.pkl", "rb"))
hb_scaler = pickle.load(open("model/hb_scaler.pkl", "rb"))
yolo_model = YOLO("model/best (2).pt")

# -------------------- AUTH ROUTES --------------------
@app.route("/signup", methods=["POST"])
def signup():
    data = request.json
    username = data.get("username")
    password = data.get("password")

    if users_collection.find_one({"username": username}):
        return jsonify({"message": "Username already exists"}), 400

    hashed_pw = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt())
    users_collection.insert_one({"username": username, "password": hashed_pw})
    return jsonify({"message": "User created successfully"}), 201


@app.route("/login", methods=["POST"])
def login():
    data = request.json
    username = data.get("username")
    password = data.get("password")

    user = users_collection.find_one({"username": username})
    if user and bcrypt.checkpw(password.encode('utf-8'), user['password']):
        return jsonify({"message": "Login successful"}), 200

    return jsonify({"message": "Invalid username or password"}), 401


# -------------------- SEGMENTATION ROUTE --------------------
@app.route("/segment", methods=["POST"])
def segment():
    try:
        image_data = request.json.get("image")
        image_bytes = base64.b64decode(image_data.split(",")[1])
        image = Image.open(BytesIO(image_bytes)).convert("RGB")
        image_np = np.array(image)

        results = yolo_model.predict(image_np, verbose=False)

        if not hasattr(results[0], "masks") or results[0].masks is None:
            return jsonify({"error": "No eyelid detected"}), 400

        mask = results[0].masks.data[0].cpu().numpy()
        mask = cv2.resize(mask, (image_np.shape[1], image_np.shape[0]))
        mask = (mask > 0.5).astype(np.uint8)
        mask = 1 - mask  # invert to get conjunctiva

        kernel = np.ones((7, 7), np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)

        overlay = np.zeros_like(image_np)
        overlay[mask == 1] = image_np[mask == 1]

        overlay_img = Image.fromarray(overlay)
        buffer = BytesIO()
        overlay_img.save(buffer, format="JPEG")
        segmented_base64 = base64.b64encode(buffer.getvalue()).decode()

        return jsonify({"segmented_image": segmented_base64})

    except Exception as e:
        return jsonify({"error": str(e)}), 400


# -------------------- PREDICTION ROUTE --------------------
@app.route("/predict", methods=["POST"])
def predict():
    try:
        image_data = request.json.get("image")
        image_bytes = base64.b64decode(image_data.split(",")[1])
        image = Image.open(BytesIO(image_bytes)).convert("RGB")
        cropped = np.array(image)

        pixels = cropped.reshape(-1, 3)

        r_mean = float(np.mean(pixels[:, 0]))
        g_mean = float(np.mean(pixels[:, 1]))
        b_mean = float(np.mean(pixels[:, 2]))

        features = np.array([[r_mean, g_mean, b_mean]])
        features_scaled = scaler.transform(features)

        pred_class_prob, pred_hb_scaled = model.predict(features_scaled, verbose=0)
        anaemia = "Yes" if pred_class_prob[0][0] > 0.5 else "No"
        hb = float(hb_scaler.inverse_transform(pred_hb_scaled)[0][0])

        return jsonify({
            "anaemia": anaemia,
            "hb": hb,
            "rgb": {"r_mean": r_mean, "g_mean": g_mean, "b_mean": b_mean}
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 400


# -------------------- CHATBOT ROUTE (GEMINI 2.5 FLASH) --------------------
@app.route("/chat", methods=["POST"])
def chat():
    try:
        user_msg = request.json.get("message")

        url = "https://openrouter.ai/api/v1/chat/completions"
        headers = {
            "Authorization": "Bearer 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",
            "Content-Type": "application/json"
        }

        payload = {
    "model": "google/gemini-2.5-flash",
    "max_tokens": 1000,   # <= FIXED (very small cost)
    "messages": [
        {"role": "system", "content": "You are AnemAi, AI doctor for anemia support."},
        {"role": "user", "content": user_msg}
    ]
}


        response = requests.post(url, json=payload, headers=headers)
        data = response.json()

        # ---------------------- ERROR HANDLING ----------------------
        if "error" in data:
            print("OPENROUTER ERROR:", data)  # shows actual error
            return jsonify({"reply": "⚠️ AnemAi Error: " + str(data["error"])})

        if "choices" not in data:
            print("INVALID RESPONSE:", data)
            return jsonify({"reply": "⚠️ Unexpected response from AI."})

        reply = data["choices"][0]["message"]["content"]

        return jsonify({"reply": reply})

    except Exception as e:
        print("SERVER ERROR:", str(e))
        return jsonify({"reply": "⚠️ Server Error: " + str(e)}), 400


# -------------------- RUN SERVER --------------------
if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5005)
