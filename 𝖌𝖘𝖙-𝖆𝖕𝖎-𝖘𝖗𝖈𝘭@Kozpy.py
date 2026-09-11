import os
import re
import logging
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from flask import Flask, jsonify, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

# ========== Configuration ==========
OWNER = os.environ.get("OWNER", "saurav")
API_KEY = os.environ.get("API_KEY", None)
PORT = int(os.environ.get("PORT", 5000))
CACHE_TTL = int(os.environ.get("CACHE_TTL", 300))
RATE_LIMIT = os.environ.get("RATE_LIMIT", "10 per minute")

RAZORPAY_GSTIN_URL = os.environ.get("RAZORPAY_GSTIN_URL", "https://razorpay.com/api/gstin")
RAZORPAY_PAN_URL = os.environ.get("RAZORPAY_PAN_URL", "https://razorpay.com/api/gstin/pan")
MASTERSINDIA_SEARCH_URL = os.environ.get("MASTERSINDIA_SEARCH_URL",
                                         "https://blog-backend.mastersindia.co/api/v1/custom/search/name_and_pan/")

USER_AGENT = os.environ.get("USER_AGENT",
                            "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Mobile Safari/537.36")

DEVELOPER_CREDIT = "Saurav - https://t.me/Number_Spy"

# ========== Logging ==========
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ========== Flask App ==========
app = Flask(__name__)

# ========== Rate Limiter ==========
limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=[RATE_LIMIT],
    storage_uri="memory://",
)

# ========== Headers ==========
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-IN,en;q=0.9,hi-IN;q=0.8,hi;q=0.7,en-GB;q=0.6,en-US;q=0.5",
}

MASTERS_HEADERS = {
    **HEADERS,
    "Origin": "https://www.mastersindia.co",
    "Referer": "https://www.mastersindia.co/gst-number-search-by-name-and-pan/",
    "Sec-Fetch-Site": "same-site",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Dest": "empty",
}

# ========== Helper Functions ==========

def add_credit(data):
    data["developer"] = DEVELOPER_CREDIT
    data["owner"] = OWNER
    return data

def is_valid_gstin(gstin):
    return bool(re.fullmatch(r'[0-9A-Z]{15}', gstin))

def is_valid_pan(pan):
    return bool(re.fullmatch(r'[A-Z]{5}[0-9]{4}[A-Z]', pan))

def safe_request(url, headers, timeout=15, method='GET', json=None):
    try:
        if method.upper() == 'GET':
            resp = requests.get(url, headers=headers, timeout=timeout)
        elif method.upper() == 'POST':
            resp = requests.post(url, headers=headers, json=json, timeout=timeout)
        else:
            raise ValueError(f"Unsupported method: {method}")
        resp.raise_for_status()
        return True, resp.json()
    except requests.exceptions.Timeout:
        logger.error(f"Timeout for {url}")
        return False, "Request timed out"
    except requests.exceptions.RequestException as e:
        logger.error(f"Request error for {url}: {e}")
        return False, f"Request failed: {str(e)}"
    except ValueError as e:
        logger.error(f"JSON decode error for {url}: {e}")
        return False, "Invalid JSON response"
    except Exception as e:
        logger.error(f"Unexpected error for {url}: {e}")
        return False, f"Unexpected error: {str(e)}"

# ========== Caching ==========
_cache = {}
_cache_timestamps = {}

def cached_request(key, fetch_func, ttl=CACHE_TTL):
    now = datetime.utcnow()
    if key in _cache and (now - _cache_timestamps[key]).total_seconds() < ttl:
        return _cache[key]
    result = fetch_func()
    _cache[key] = result
    _cache_timestamps[key] = now
    return result

def fetch_razorpay_gstin(gstin):
    success, data = safe_request(f"{RAZORPAY_GSTIN_URL}/{gstin}", HEADERS, timeout=10)
    return {"source": "razorpay", "gstin": gstin, "data": data if success else {"error": data}}

def fetch_razorpay_pan(pan):
    success, data = safe_request(f"{RAZORPAY_PAN_URL}/{pan}", HEADERS, timeout=10)
    if success:
        return {
            "source": "razorpay",
            "pan": pan,
            "total_gstins": data.get("count", 0),
            "gstins": data.get("items", []),
            "data": data
        }
    else:
        return {"source": "razorpay", "pan": pan, "error": data}

def fetch_mastersindia_name(name):
    keyword = name.strip().replace(" ", "+") + "+"
    url = f"{MASTERSINDIA_SEARCH_URL}?keyword={keyword}"
    success, data = safe_request(url, MASTERS_HEADERS, timeout=20)
    if success and data.get("success") and data.get("data"):
        gstins = []
        for item in data["data"]:
            gstins.append({
                "gstin": item.get("gstin", ""),
                "legal_name": item.get("lgnm", ""),
                "trade_name": item.get("tradeNam", ""),
                "status": item.get("sts", ""),
                "taxpayer_type": item.get("dty", ""),
                "constitution": item.get("ctb", ""),
                "registration_date": item.get("rgdt", ""),
                "state_jurisdiction": item.get("stj", ""),
                "state_code": item.get("stjCd", ""),
                "central_jurisdiction": item.get("ctj", ""),
                "central_code": item.get("ctjCd", ""),
                "einvoice_status": item.get("einvoiceStatus", ""),
                "nature_of_business": item.get("nba", []),
                "principal_address": item.get("pradr", {}),
                "additional_addresses": item.get("adadr", []),
                "last_updated": item.get("lstupdt", ""),
                "cancelled_date": item.get("cxdt", ""),
            })
        return {
            "source": "mastersindia",
            "query": name,
            "total_gstins": len(gstins),
            "gstins": gstins
        }
    else:
        return {"source": "mastersindia", "query": name, "error": "No data or API error"}

def fetch_mastersindia_active_cancelled(name):
    result = fetch_mastersindia_name(name)
    if "error" in result:
        return result
    active = []
    cancelled = []
    for g in result.get("gstins", []):
        entry = {
            "gstin": g["gstin"],
            "legal_name": g["legal_name"],
            "trade_name": g["trade_name"],
            "state_code": g["state_code"],
            "status": g["status"],
        }
        if g["status"] == "Active":
            active.append(entry)
        else:
            cancelled.append(entry)
    return {
        "source": "mastersindia",
        "query": name,
        "total": len(active) + len(cancelled),
        "active_gstins": active,
        "cancelled_gstins": cancelled
    }

# ========== Authentication ==========
def require_auth():
    if API_KEY:
        token = request.headers.get("X-API-Key")
        if not token or token != API_KEY:
            return False
    return True

def auth_decorator(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not require_auth():
            return jsonify(add_credit({"error": "Authentication required"})), 401
        return f(*args, **kwargs)
    return decorated

# ========== Routes ==========

@app.route("/", methods=["GET"])
@limiter.limit("5 per minute")
def home():
    return jsonify(add_credit({
        "app": "GST API Suite (Enhanced)",
        "version": "4.0",
        "endpoints": {
            "1. GET /gstin/<gstin>": "GSTIN Info (Razorpay)",
            "2. GET /pan/<pan>": "PAN -> All GSTINs (Razorpay)",
            "3. GET /gst-to-pan/<gstin>": "GSTIN -> PAN (extract and query)",
            "4. GET /name-to-gstin?name=<company>": "Name -> Detailed GSTINs (MastersIndia)",
            "5. GET /search-gstin?name=<company>": "Name -> Active/Cancelled lists (MastersIndia)",
            "6. GET /gstin-detail/<gstin>": "GSTIN Detail (Razorpay) + note",
            "7. GET /all-search": "Combined multi-source search (name, gstin, pan)",
        },
        "authentication": "Set API_KEY env var to enable; then send X-API-Key header",
        "rate_limit": f"Default: {RATE_LIMIT} per IP",
        "caching": f"TTL = {CACHE_TTL}s",
        "example_calls": {
            "local": "http://localhost:5000/name-to-gstin?name=Amazon",
            "render": "https://your-app.onrender.com/name-to-gstin?name=Flipkart"
        }
    }))

@app.route("/gstin/<gstin>", methods=["GET"])
@limiter.limit(RATE_LIMIT)
@auth_decorator
def gstin_info(gstin):
    if not is_valid_gstin(gstin):
        return jsonify(add_credit({"error": "Invalid GSTIN format. Must be 15 alphanumeric characters."})), 400

    def fetch():
        return fetch_razorpay_gstin(gstin)
    cache_key = f"razorpay_gstin_{gstin}"
    result = cached_request(cache_key, fetch)
    return jsonify(add_credit(result))

@app.route("/pan/<pan>", methods=["GET"])
@limiter.limit(RATE_LIMIT)
@auth_decorator
def pan_to_gst(pan):
    if not is_valid_pan(pan):
        return jsonify(add_credit({"error": "Invalid PAN format. Must be 5 letters, 4 digits, 1 letter."})), 400

    def fetch():
        return fetch_razorpay_pan(pan)
    cache_key = f"razorpay_pan_{pan}"
    result = cached_request(cache_key, fetch)
    return jsonify(add_credit(result))

@app.route("/gst-to-pan/<gstin>", methods=["GET"])
@limiter.limit(RATE_LIMIT)
@auth_decorator
def gst_to_pan(gstin):
    if not is_valid_gstin(gstin):
        return jsonify(add_credit({"error": "Invalid GSTIN format"})), 400
    pan = gstin[2:12]
    return pan_to_gst(pan)

@app.route("/name-to-gstin", methods=["GET"])
@limiter.limit(RATE_LIMIT)
@auth_decorator
def name_to_gstin():
    name = request.args.get("name", "").strip()
    if not name:
        return jsonify(add_credit({"error": "Missing 'name' parameter"})), 400

    def fetch():
        return fetch_mastersindia_name(name)
    cache_key = f"masters_name_{name.lower()}"
    result = cached_request(cache_key, fetch)
    return jsonify(add_credit(result))

@app.route("/search-gstin", methods=["GET"])
@limiter.limit(RATE_LIMIT)
@auth_decorator
def search_gstin():
    name = request.args.get("name", "").strip()
    if not name:
        return jsonify(add_credit({"error": "Missing 'name' parameter"})), 400

    def fetch():
        return fetch_mastersindia_active_cancelled(name)
    cache_key = f"masters_active_{name.lower()}"
    result = cached_request(cache_key, fetch)
    return jsonify(add_credit(result))

@app.route("/gstin-detail/<gstin>", methods=["GET"])
@limiter.limit(RATE_LIMIT)
@auth_decorator
def gstin_detail_masters(gstin):
    if not is_valid_gstin(gstin):
        return jsonify(add_credit({"error": "Invalid GSTIN format"})), 400

    def fetch():
        razorpay = fetch_razorpay_gstin(gstin)
        return {
            "gstin": gstin,
            "razorpay_info": razorpay,
            "note": "MastersIndia supports name search only; use /name-to-gstin for company name."
        }
    cache_key = f"detail_{gstin}"
    result = cached_request(cache_key, fetch)
    return jsonify(add_credit(result))

@app.route("/all-search", methods=["GET"])
@limiter.limit(RATE_LIMIT)
@auth_decorator
def all_search():
    name = request.args.get("name", "").strip()
    gstin = request.args.get("gstin", "").strip().upper()
    pan = request.args.get("pan", "").strip().upper()

    if gstin and not is_valid_gstin(gstin):
        return jsonify(add_credit({"error": "Invalid GSTIN format"})), 400
    if pan and not is_valid_pan(pan):
        return jsonify(add_credit({"error": "Invalid PAN format"})), 400

    results = {}
    tasks = []

    with ThreadPoolExecutor(max_workers=3) as executor:
        if gstin:
            tasks.append(executor.submit(fetch_razorpay_gstin, gstin))
        if pan:
            tasks.append(executor.submit(fetch_razorpay_pan, pan))
        if name:
            tasks.append(executor.submit(fetch_mastersindia_name, name))

        for future in as_completed(tasks):
            try:
                result = future.result()
                if result.get("source") == "razorpay":
                    if "gstin" in result and result.get("gstin"):
                        results["gstin_info_razorpay"] = result
                    elif "pan" in result:
                        results["pan_search_razorpay"] = result
                elif result.get("source") == "mastersindia":
                    results["name_search_mastersindia"] = result
            except Exception as e:
                logger.error(f"Parallel task error: {e}")

    return jsonify(add_credit({
        "query_params": {"name": name, "gstin": gstin, "pan": pan},
        "results": results
    }))

# ========== Error Handlers ==========
@app.errorhandler(429)
def ratelimit_handler(e):
    return jsonify(add_credit({"error": "Rate limit exceeded. Please slow down."})), 429

@app.errorhandler(404)
def not_found(e):
    return jsonify(add_credit({"error": "Endpoint not found"})), 404

@app.errorhandler(500)
def internal_error(e):
    logger.error(f"Internal server error: {e}")
    return jsonify(add_credit({"error": "Internal server error"})), 500

# ========== Main ==========
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=False)