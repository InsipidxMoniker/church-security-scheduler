import ipaddress
import os
import secrets
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import pyotp
from cryptography.fernet import InvalidToken
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.exc import IntegrityError, OperationalError

from scheduler.auth import Auth
from scheduler import __version__
from scheduler.client_ip import client_ip as resolved_client_ip
from scheduler.db import Database, audit, row
from scheduler.security import Problem, Security, digest, password_policy, token

STAFF = "__Host-staff"
PENDING = "__Host-pending"
CSRF = "__Host-csrf"
PREAUTH = "__Host-preauth"
templates = Jinja2Templates(directory="scheduler/templates")
templates.env.filters["utc"] = lambda value: datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if value is not None else "Not set"


class Form(BaseModel):
    model_config = ConfigDict(extra="forbid")
    csrf: str = Field(min_length=1, max_length=2048)


class Login(Form):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=256)


class MFA(Form):
    code: str = Field(min_length=1, max_length=128)
    recovery: bool = False


class NewStaff(Login):
    role: str


class ChangeStaff(Form):
    expected_epoch: int = Field(ge=1)
    role: str
    active: bool


class ResetStaff(Form):
    expected_epoch: int = Field(ge=1)
    password: str = Field(min_length=15, max_length=256)


async def form(request, model):
    try:
        body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else dict(await request.form())
        return model.model_validate(body)
    except (ValidationError, ValueError):
        raise Problem(422, "Invalid request fields") from None


def cookie(response, name, value, age):
    response.set_cookie(name, value, max_age=age, secure=True, httponly=True, samesite="lax", path="/")


def clear_cookie(response, name):
    response.delete_cookie(name, secure=True, httponly=True, samesite="lax", path="/")


def wants_json(request):
    return "application/json" in request.headers.get("content-type", "") or "application/json" in request.headers.get("accept", "")


def require_same_origin(request, public_origin):
    """Check browser provenance against configuration, never the upstream URL.

    With no-referrer, Firefox native form POSTs have an opaque/missing Origin
    and no Referer. Accept that privacy case only with the browser-controlled
    Sec-Fetch-Site: same-origin signal; handlers still require their CSRF token.
    A concrete foreign Origin can never be rescued by another header.
    """
    origins = request.headers.getlist("origin")
    sites = request.headers.getlist("sec-fetch-site")
    if len(origins) > 1 or len(sites) > 1:
        raise Problem(403, "Same-origin request required")
    if sites and sites[0] != "same-origin":
        raise Problem(403, "Same-origin request required")
    if origins == [public_origin]:
        return
    if origins not in ([], ["null"]) or sites != ["same-origin"]:
        raise Problem(403, "Same-origin request required")
    # Referer is normally absent under no-referrer. If supplied, it must not
    # contradict the same-origin evidence. It is never used as a sole fallback.
    referers = request.headers.getlist("referer")
    if referers:
        if len(referers) != 1 or any(ord(c) <= 32 for c in referers[0]):
            raise Problem(403, "Same-origin request required")
        try:
            source, target = urlsplit(referers[0]), urlsplit(public_origin)
            if source.scheme != target.scheme or source.netloc != target.netloc:
                raise ValueError
        except ValueError:
            raise Problem(403, "Same-origin request required") from None


def create_app(db=None, security=None, origin=None, clock=None):
    db = db or Database()
    origin = origin or os.environ.get("APP_ORIGIN", "https://localhost:8443")
    if urlsplit(origin).scheme != "https" or urlsplit(origin).path:
        raise RuntimeError("APP_ORIGIN must be an HTTPS origin without a path")
    trusted = [ipaddress.ip_network(v.strip()) for v in os.environ.get("TRUSTED_PROXIES", "").split(",") if v.strip()]
    max_body_bytes = int(os.environ.get("MAX_BODY_BYTES", "1048576"))
    if max_body_bytes <= 0:
        raise RuntimeError("MAX_BODY_BYTES must be positive")

    @asynccontextmanager
    async def lifespan(app):
        try:
            db.check()
            app.state.security = security or Security(os.environ.get("TOTP_KEY_FILE", "/keys/totp.key"))
            app.state.auth = Auth(db, app.state.security, **({"clock": clock} if clock else {}))
            from scheduler.volunteer_flow import VolunteerFlow
            app.state.flow = VolunteerFlow(db, app.state.security, app.state.auth)
            from scheduler.generation import Generation
            app.state.generation = Generation(db, app.state.flow)
            from scheduler.publication import Publication
            app.state.publication = Publication(db, app.state.flow, app.state.generation)
            yield
        finally:
            db.engine.dispose()

    app = FastAPI(version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.db = db

    def error(problem, request=None):
        if request is not None and not wants_json(request) and request.url.path.startswith(("/availability/", "/staff/settings", "/staff/volunteers", "/staff/dates")):
            path = request.url.path
            if path.startswith("/availability/"):
                pieces = path.split("/")
                back = "/".join(pieces[:3]) + ("/mine" if pieces[-1] == "mine" and problem.status != 401 else "")
            elif path.startswith("/staff/volunteers/import"):
                back = "/staff/volunteers/import"
            elif path.startswith("/staff/volunteers/") and path.endswith("/pin/view"):
                back = path.rsplit("/", 2)[0]
            elif path.startswith("/staff/volunteers/") and path.split("/")[-1] in ("pin", "clear-block"):
                back = path.rsplit("/", 1)[0]
            elif path.startswith("/staff/dates/"):
                back = "/".join(path.split("/")[:4])
            else:
                back = path
            return templates.TemplateResponse(request=request, name="phase2_error.html", context={"message": problem.message, "back": back}, status_code=problem.status, headers={"Retry-After": str(problem.retry)} if problem.retry else None)
        return JSONResponse({"detail": problem.message}, status_code=problem.status,
                            headers={"Retry-After": str(problem.retry)} if problem.retry else None)

    @app.middleware("http")
    async def controls(request, call_next):
        try:
            if request.url.path not in ("/health/live", "/health/ready") and request.headers.get("host") != urlsplit(origin).netloc:
                raise Problem(400, "Invalid host")
            if request.method not in ("GET", "HEAD", "OPTIONS"):
                require_same_origin(request, origin)
            # Count streamed/chunked content too; Content-Length alone is not trusted.
            size = 0
            chunks = []
            async for chunk in request.stream():
                size += len(chunk)
                if size > max_body_bytes:
                    raise Problem(413, "Request body is too large")
                chunks.append(chunk)
            request._body = b"".join(chunks)
            response = await call_next(request)
        except Problem as exc:
            response = error(exc, request)
        except IntegrityError:
            response = error(Problem(409, "Change conflicts with current state; refresh and retry"), request)
        except OperationalError:
            response = error(Problem(503, "Database temporarily unavailable; retry shortly", 5), request)
        except Exception:
            # Never log exception values, SQL parameters, requests, or credential payloads.
            response = error(Problem(500, "Request could not be completed"))
        response.headers.update({
            "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "default-src 'self'; script-src 'none'; style-src 'self'; img-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
            "X-Content-Type-Options": "nosniff", "Strict-Transport-Security": "max-age=31536000",
        })
        return response

    @app.exception_handler(Problem)
    async def problem_handler(request, exc):
        return error(exc, request)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request, exc):
        return error(Problem(422, "Invalid request fields"), request)

    def auth(request):
        return request.app.state.auth

    def client_ip(request):
        return resolved_client_ip(request, trusted)

    def staff(c, request, csrf=None, role=None):
        return auth(request).session(c, request.cookies.get(STAFF), csrf=csrf, role=role)[0]

    def view(request, template_name, **context):
        return templates.TemplateResponse(request=request, name=template_name, context={"csrf": request.cookies.get(CSRF, ""), **context})

    @app.get("/health/live")
    def live():
        return {"status": "ok"}

    @app.get("/health/ready")
    def ready():
        try:
            db.check()
        except Exception:
            raise Problem(503, "Not ready") from None
        return {"status": "ready"}

    @app.get("/")
    def home():
        return RedirectResponse("/staff", 303)

    @app.get("/login")
    def login_page(request: Request):
        raw = app.state.security.cipher.encrypt(token().encode()).decode()
        response = view(request, "login.html", csrf=raw)
        cookie(response, PREAUTH, raw, 600)
        return response

    @app.post("/login")
    async def login(request: Request):
        data = await form(request, Login)
        raw = request.cookies.get(PREAUTH, "")
        try:
            app.state.security.cipher.decrypt(raw.encode(), ttl=600)
        except InvalidToken:
            raise Problem(403, "Refresh the sign-in page") from None
        if not secrets.compare_digest(digest(raw), digest(data.csrf)):
            raise Problem(403, "Invalid CSRF token")
        # CPU-bound hashing runs in the bounded thread pool, never the event loop.
        from starlette.concurrency import run_in_threadpool
        pending, csrf = await run_in_threadpool(auth(request).login, data.username, data.password, client_ip(request))
        response = JSONResponse({"next": "/mfa"}) if wants_json(request) else RedirectResponse("/mfa", 303)
        cookie(response, PENDING, pending, 600)
        cookie(response, CSRF, csrf, 600)
        clear_cookie(response, PREAUTH)
        return response

    @app.get("/mfa")
    def mfa_page(request: Request):
        with db.transaction() as c:
            user, _ = auth(request).session(c, request.cookies.get(PENDING), "pending")
        seed = app.state.security.seed(user) if not user["enrolled"] else None
        uri = pyotp.TOTP(seed).provisioning_uri(user["username"], issuer_name="Church Security Scheduler") if seed else None
        if wants_json(request):
            return {"enrollment_required": not user["enrolled"], "seed": seed, "uri": uri}
        return view(request, "mfa.html", seed=seed)

    @app.post("/mfa")
    async def mfa(request: Request):
        data = await form(request, MFA)
        from starlette.concurrency import run_in_threadpool
        raw, csrf, codes = await run_in_threadpool(auth(request).mfa, request.cookies.get(PENDING), data.csrf, data.code, data.recovery, client_ip(request))
        response = JSONResponse({"recovery_codes": codes, "next": "/staff"}) if wants_json(request) else (view(request, "recovery.html", codes=codes, csrf=csrf) if codes else RedirectResponse("/staff", 303))
        cookie(response, STAFF, raw, 28800)
        cookie(response, CSRF, csrf, 28800)
        clear_cookie(response, PENDING)
        return response

    @app.post("/logout")
    async def logout(request: Request):
        data = await form(request, Form)
        with db.transaction() as c:
            user = staff(c, request, data.csrf)
            c.exec_driver_sql("UPDATE staff_sessions SET revoked_at=? WHERE token_hash=?", (auth(request).clock(), digest(request.cookies[STAFF])))
            audit(c, "staff.logout", user["id"], actor=user["id"])
        response = JSONResponse({"next": "/login"}) if wants_json(request) else RedirectResponse("/login", 303)
        for name in (STAFF, PENDING, CSRF):
            clear_cookie(response, name)
        return response

    @app.get("/staff")
    def dashboard(request: Request):
        with db.transaction() as c:
            user = staff(c, request)
            settings = row(c, "SELECT * FROM settings WHERE id=1")
            positions = [dict(r) for r in c.exec_driver_sql("SELECT * FROM positions ORDER BY number").mappings()]
        if wants_json(request):
            return {"username": user["username"], "role": user["role"], "version": __version__, "settings": settings, "positions": positions}
        return view(request, "staff.html", user=user, settings=settings, positions=positions)

    @app.get("/staff/health")
    def health(request: Request):
        with db.transaction() as c:
            staff(c, request)
            backup = row(c, "SELECT action,occurred_at FROM audit_events WHERE action IN ('backup.completed','backup.failed') ORDER BY occurred_at DESC,rowid DESC LIMIT 1")
            last_success = c.exec_driver_sql("SELECT max(occurred_at) FROM audit_events WHERE action='backup.completed'").scalar()
        db.check()
        from scheduler.db import HEAD
        return {"schema": HEAD, "database": "ready", "version": __version__, "backup_status": backup or {"action": "never_recorded"}, "last_successful_backup": last_success}

    @app.get("/staff/audit")
    def audit_page(request: Request, page: int = 1):
        if not 1 <= page <= 1000000:
            raise Problem(422, "Invalid page")
        with db.transaction() as c:
            staff(c, request)
            rows = [dict(r) for r in c.exec_driver_sql("SELECT occurred_at,action,object_id,actor_type,staff_id,details FROM audit_events ORDER BY occurred_at DESC,id LIMIT 50 OFFSET ?", ((page - 1) * 50,)).mappings()]
        if wants_json(request):
            return {"events": rows, "page": page}
        return view(request, "audit.html", events=rows, page=page)

    @app.get("/staff/users")
    def users(request: Request, page: int = 1):
        if not 1 <= page <= 1000000:
            raise Problem(422, "Invalid page")
        with db.transaction() as c:
            staff(c, request, role="admin")
            rows = [dict(r) for r in c.exec_driver_sql("SELECT id,username,role,active,enrolled,credential_epoch FROM admin_users ORDER BY username LIMIT 50 OFFSET ?", ((page - 1) * 50,)).mappings()]
        if wants_json(request):
            return {"users": rows, "page": page}
        return view(request, "users.html", users=rows, page=page)

    @app.post("/staff/users")
    async def new_user(request: Request):
        data = await form(request, NewStaff)
        with db.transaction() as c:
            staff(c, request, data.csrf, "admin")
        password_policy(data.password)
        from starlette.concurrency import run_in_threadpool
        encoded = await run_in_threadpool(app.state.security.hash_password, data.password)
        with db.transaction() as c:
            actor = staff(c, request, data.csrf, "admin")
            user_id = auth(request).create_user(c, data.username, encoded, data.role, actor["id"])
        return JSONResponse({"id": user_id}, 201) if wants_json(request) else RedirectResponse("/staff/users", 303)

    @app.post("/staff/users/{user_id}/access")
    async def access(user_id: str, request: Request):
        data = await form(request, ChangeStaff)
        if data.role not in ("admin", "scheduler"):
            raise Problem(422, "Invalid role")
        with db.transaction() as c:
            actor = staff(c, request, data.csrf, "admin")
            target = row(c, "SELECT * FROM admin_users WHERE id=?", (user_id,))
            if not target:
                raise Problem(404, "Staff account not found")
            if target["credential_epoch"] != data.expected_epoch:
                raise Problem(409, "Account changed; refresh before retrying")
            c.exec_driver_sql("UPDATE admin_users SET role=?,active=?,credential_epoch=credential_epoch+1,updated_at=? WHERE id=?", (data.role, int(data.active), auth(request).clock(), user_id))
            auth(request).revoke(c, user_id)
            audit(c, "staff.access_changed", user_id, actor=actor["id"], details={"before": {"role": target["role"], "active": target["active"]}, "after": {"role": data.role, "active": data.active}, "credential_epoch": data.expected_epoch + 1})
        return JSONResponse({"updated": True}) if wants_json(request) else RedirectResponse("/staff/users", 303)

    @app.post("/staff/users/{user_id}/reset")
    async def reset(user_id: str, request: Request):
        data = await form(request, ResetStaff)
        with db.transaction() as c:
            staff(c, request, data.csrf, "admin")
        from starlette.concurrency import run_in_threadpool
        encoded = await run_in_threadpool(app.state.security.hash_password, data.password)
        with db.transaction() as c:
            actor = staff(c, request, data.csrf, "admin")
            target = row(c, "SELECT * FROM admin_users WHERE id=?", (user_id,))
            if not target:
                raise Problem(404, "Staff account not found")
            if target["credential_epoch"] != data.expected_epoch:
                raise Problem(409, "Account changed; refresh before retrying")
            auth(request).reset(c, user_id, encoded, actor["id"])
        return JSONResponse({"enrollment_required": True}) if wants_json(request) else RedirectResponse("/staff/users", 303)

    @app.post("/staff/users/{user_id}/clear-block")
    async def clear_block(user_id: str, request: Request):
        data = await form(request, Form)
        with db.transaction() as c:
            actor = staff(c, request, data.csrf, "admin")
            user = row(c, "SELECT username FROM admin_users WHERE id=?", (user_id,))
            if not user:
                raise Problem(404, "Staff account not found")
            c.exec_driver_sql("DELETE FROM auth_throttles WHERE scope_hash=?", (digest("account:" + user["username"]),))
            audit(c, "staff.block_cleared", user_id, actor=actor["id"])
        return JSONResponse({"cleared": True}) if wants_json(request) else RedirectResponse("/staff/users", 303)

    from scheduler.phase2_routes import register_phase2
    register_phase2(app, origin, client_ip, view)
    from scheduler.schedule_routes import register_schedules
    register_schedules(app, view)
    app.mount("/static", StaticFiles(directory="scheduler/static"), name="static")
    return app
