import hashlib
import secrets
import threading
import time
from pathlib import Path

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from cryptography.fernet import Fernet


class Problem(Exception):
    def __init__(self, status, message, retry=None):
        self.status, self.message, self.retry = status, message, retry


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def token():
    return secrets.token_urlsafe(32)


class Security:
    def __init__(self, key_file):
        self.cipher = Fernet(Path(key_file).read_bytes().strip())
        self.hasher = PasswordHasher(memory_cost=19456, time_cost=2, parallelism=1)
        self.hash_slots = threading.BoundedSemaphore(2)
        self.dummy_hash = self.hash_password(token())

    def hash_password(self, password):
        if not self.hash_slots.acquire(blocking=False):
            raise Problem(429, "Authentication busy; retry shortly", 2)
        try:
            return self.hasher.hash(password)
        finally:
            self.hash_slots.release()

    def verify(self, encoded, password):
        if not self.hash_slots.acquire(blocking=False):
            raise Problem(429, "Authentication busy; retry shortly", 2)
        try:
            try:
                return self.hasher.verify(encoded, password)
            except (VerificationError, InvalidHashError):
                return False
        finally:
            self.hash_slots.release()

    def new_seed(self):
        return self.cipher.encrypt(pyotp.random_base32().encode()).decode()

    def seed(self, user):
        return self.cipher.decrypt(user["totp_secret"].encode()).decode()

    def totp_step(self, user, code, now):
        if len(code) != 6 or not code.isascii() or not code.isdecimal():
            return None
        otp = pyotp.TOTP(self.seed(user))
        for step in range(int(now // 30) - 1, int(now // 30) + 2):
            if step > user["last_totp_step"] and secrets.compare_digest(otp.at(step * 30), code):
                return step
        return None


def password_policy(password):
    if not 15 <= len(password) <= 256:
        raise Problem(422, "Password must contain 15–256 characters")


def username_policy(username):
    username = username.strip().casefold()
    if not 1 <= len(username) <= 100 or any(ord(c) < 32 or ord(c) == 127 for c in username):
        raise Problem(422, "Invalid username")
    return username
