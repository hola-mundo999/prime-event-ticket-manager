import base64
import hashlib
import hmac
import struct
import time

import auth
import main


def make_totp(secret, timestamp=None):
    padded = secret + "=" * ((8 - len(secret) % 8) % 8)
    key = base64.b32decode(padded)
    counter = int((time.time() if timestamp is None else timestamp) // 30)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 15
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{value % 1_000_000:06d}"


def csrf_headers(client):
    token = client.cookies.get("prime_admin_csrf")
    return {"X-CSRF-Token": token} if token else {}


def authorize_admin(client, email=auth.ADMIN_EMAIL, password="valid-admin-password-1"):
    response = client.post(
        "/api/admin/login", json={"email": email, "password": password}
    )
    if response.status_code != 200:
        return response
    if response.json()["enrollment_required"]:
        enrollment = client.post("/api/admin/mfa/enroll/start")
        if enrollment.status_code != 200:
            return enrollment
        activation = client.post(
            "/api/admin/mfa/enroll/activate",
            json={"code": make_totp(enrollment.json()["secret"])},
        )
        return activation
    user = main.store.find_user(email)
    secret = auth.decrypt_totp_secret(user["totp_secret"])
    return client.post(
        "/api/admin/mfa/verify", json={"code": make_totp(secret)}
    )
