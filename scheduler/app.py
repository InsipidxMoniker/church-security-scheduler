import ipaddress
import os
import re
import secrets
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import pyotp
from cryptography.fernet import InvalidToken
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response
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
HTML_PAGES = frozenset({
    "/mfa", "/staff", "/staff/users", "/staff/audit", "/staff/health",
    "/staff/settings", "/staff/volunteers", "/staff/volunteers/new",
    "/staff/volunteers/import", "/staff/volunteers/{volunteer_id}",
    "/staff/dates", "/staff/dates/{date_id}", "/staff/dates/{date_id}/draft",
    "/staff/dates/{date_id}/publications/{version_id}",
    "/staff/users/{user_id}/delete", "/staff/volunteers/{volunteer_id}/delete",
})
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


class DeleteStaff(Form):
    expected_epoch: int = Field(ge=1)
    confirmed: bool


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


def explicit_html(request):
    """Opt in to new HTML behavior; JSON keeps its existing precedence."""
    accept = ",".join(request.headers.getlist("accept")).lower()
    if wants_json(request) or "application/json" in accept or "application/json" in request.headers.get("content-type", "").lower():
        return False
    for item in accept.split(","):
        media, *parameters = [part.strip() for part in item.split(";")]
        if media != "text/html":
            continue
        qualities = [part.partition("=")[2].strip() for part in parameters if part.partition("=")[0].strip() == "q"]
        if not qualities:
            return True
        if len(qualities) == 1 and re.fullmatch(r"(?:0(?:\.\d{0,3})?|1(?:\.0{0,3})?)", qualities[0]) and float(qualities[0]) > 0:
            return True
    return False


def page_request(request):
    return request.method == "GET" and getattr(request.scope.get("route"), "path", None) in HTML_PAGES


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
        if request is not None and page_request(request) and explicit_html(request):
            if problem.status == 401 and problem.message == "Sign in required":
                return RedirectResponse("/login", 303)
            if request.url.path == "/staff/health" and problem.status >= 500:
                return templates.TemplateResponse(request=request, name="health.html", context={"health": None}, status_code=problem.status, headers={"Retry-After": str(problem.retry)} if problem.retry else None)
        if request is not None and not wants_json(request) and request.url.path.startswith(("/availability/", "/staff/settings", "/staff/users", "/staff/volunteers", "/staff/dates")):
            path = request.url.path
            if path.startswith("/availability/"):
                pieces = path.split("/")
                back = "/".join(pieces[:3]) + ("/mine" if pieces[-1] == "mine" and problem.status != 401 else "")
            elif path.startswith("/staff/volunteers/import"):
                back = "/staff/volunteers/import"
            elif path == "/staff/volunteers":
                back = "/staff/volunteers/new"
            elif path.startswith("/staff/users"):
                back = "/staff/users"
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
        if page_request(request) or (request.method == "POST" and request.url.path in ("/login", "/mfa")):
            response.headers.add_vary_header("Accept")
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

    def login_response(request, *, status=200, message=None, username="", retry=None):
        raw = app.state.security.cipher.encrypt(token().encode()).decode()
        response = templates.TemplateResponse(request=request, name="login.html",
            context={"csrf": raw, "message": message, "username": username}, status_code=status,
            headers={"Retry-After": str(retry)} if retry else None)
        cookie(response, PREAUTH, raw, 600)
        return response

    @app.get("/login")
    def login_page(request: Request):
        return login_response(request)

    @app.post("/login")
    async def login(request: Request):
        data = None
        try:
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
        except Problem as exc:
            if not explicit_html(request) or exc.status not in (401, 403, 422, 429):
                raise
            message = ("Invalid username or password. Try again." if exc.status == 401 else
                       "Check the sign-in fields and try again." if exc.status == 422 else exc.message)
            # Fresh preauth CSRF supports retry even after an expired form, but
            # neither pending nor authenticated sessions are issued on failure.
            username = data.username if data and not any(ord(ch) < 32 or ord(ch) == 127 for ch in data.username) else ""
            return login_response(request, status=exc.status, message=message,
                                  username=username, retry=exc.retry)
        response = JSONResponse({"next": "/mfa"}) if wants_json(request) else RedirectResponse("/mfa", 303)
        cookie(response, PENDING, pending, 600)
        cookie(response, CSRF, csrf, 600)
        clear_cookie(response, PREAUTH)
        return response

    def enrollment(request):
        with db.transaction() as c:
            user, _ = auth(request).session(c, request.cookies.get(PENDING), "pending")
        seed = app.state.security.seed(user) if not user["enrolled"] else None
        uri = pyotp.TOTP(seed).provisioning_uri(user["username"], issuer_name="Church Security Scheduler") if seed else None
        return {"enrollment_required": not user["enrolled"], "seed": seed, "uri": uri}

    @app.get("/mfa")
    def mfa_page(request: Request):
        data = enrollment(request)
        if wants_json(request):
            return data
        return view(request, "mfa.html", seed=data["seed"])

    @app.get("/mfa/qr.svg")
    def enrollment_qr(request: Request):
        data = enrollment(request)
        if not data["enrollment_required"]:
            raise Problem(403, "Enrollment is not available")
        from reportlab.graphics import renderSVG
        from reportlab.graphics.barcode.qr import QrCodeWidget
        from reportlab.graphics.shapes import Drawing, Rect
        from reportlab.lib.colors import black, white
        # Four-module quiet zone, solid white background, no text/links/metadata
        # containing the provisioning URI. Generate in memory, never static files.
        qr = QrCodeWidget(data["uri"], barBorder=4, barFillColor=black, barStrokeColor=black)
        x1, y1, x2, y2 = qr.getBounds()
        scale = 320 / (x2 - x1)
        drawing = Drawing(320, 320, transform=[scale, 0, 0, scale, -x1 * scale, -y1 * scale])
        drawing.add(Rect(x1, y1, x2 - x1, y2 - y1, fillColor=white, strokeColor=None))
        drawing.add(qr)
        return Response(renderSVG.drawToString(drawing), media_type="image/svg+xml")

    @app.post("/mfa")
    async def mfa(request: Request):
        data = await form(request, MFA)
        from starlette.concurrency import run_in_threadpool
        try:
            raw, csrf, codes = await run_in_threadpool(auth(request).mfa, request.cookies.get(PENDING), data.csrf, data.code, data.recovery, client_ip(request))
        except Problem as exc:
            if not explicit_html(request) or exc.status not in (401, 429):
                raise
            # Revalidate the existing pending session before showing any enrollment
            # material. Expired/revoked sessions must begin password sign-in again.
            if exc.status == 401 and exc.message == "Sign in required":
                return login_response(request, status=401, message="Your sign-in expired. Sign in again.")
            enrollment_data = await run_in_threadpool(enrollment, request)
            return templates.TemplateResponse(request=request, name="mfa.html",
                context={"csrf": request.cookies.get(CSRF, ""), "seed": enrollment_data["seed"],
                         "message": "Invalid verification code. Try again." if exc.status == 401 else exc.message,
                         "recovery_error": data.recovery}, status_code=exc.status,
                headers={"Retry-After": str(exc.retry)} if exc.retry else None)
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

    def health_data(request):
        with db.transaction() as c:
            staff(c, request)
            backup = row(c, "SELECT action,occurred_at FROM audit_events WHERE action IN ('backup.completed','backup.failed') ORDER BY occurred_at DESC,rowid DESC LIMIT 1")
            last_success = c.exec_driver_sql("SELECT max(occurred_at) FROM audit_events WHERE action='backup.completed'").scalar()
        db.check()
        from scheduler.db import HEAD
        return {"schema": HEAD, "database": "ready", "version": __version__, "backup_status": backup or {"action": "never_recorded"}, "last_successful_backup": last_success}

    @app.get("/staff/health")
    def health(request: Request):
        try:
            data = health_data(request)
        except RuntimeError:
            # Preserve the existing JSON error status; HTML gets a useful,
            # sanitized unavailable page. Never expose exception diagnostics.
            if explicit_html(request):
                raise Problem(503, "System health temporarily unavailable", 5) from None
            raise
        if explicit_html(request):
            return view(request, "health.html", health=data)
        return data

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
    def users(request: Request, page: int = 1, status: str = "active", removed: bool = False):
        if not 1 <= page <= 1000000:
            raise Problem(422, "Invalid page")
        if status not in ("active", "inactive", "all"):
            raise Problem(422, "Invalid staff status")
        with db.transaction() as c:
            actor = staff(c, request, role="admin")
            where = "" if status == "all" else " WHERE active=?"
            args = () if status == "all" else (int(status == "active"),)
            rows = [dict(r) for r in c.exec_driver_sql("SELECT id,username,role,active,enrolled,credential_epoch FROM admin_users" + where + " ORDER BY username LIMIT 50 OFFSET ?", (*args, (page - 1) * 50)).mappings()]
        if wants_json(request):
            return {"users": rows, "page": page, "status": status}
        return view(request, "users.html", users=rows, page=page, status=status, removed=removed, actor_id=actor["id"])

    @app.get("/staff/users/{user_id}/delete")
    def confirm_staff_removal(user_id: str, request: Request):
        with db.transaction() as c:
            staff(c, request, role="admin")
            target = row(c, "SELECT id,username,credential_epoch FROM admin_users WHERE id=?", (user_id,))
            if not target:
                raise Problem(404, "Staff account not found")
        return view(request, "confirm_removal.html", name=target["username"], kind="staff account", action=f"/staff/users/{user_id}/delete", cancel="/staff/users", revision_field="expected_epoch", revision=target["credential_epoch"])

    @app.post("/staff/users/{user_id}/delete")
    async def delete_staff(user_id: str, request: Request):
        data = await form(request, DeleteStaff)
        with db.transaction() as c:
            actor = staff(c, request, data.csrf, "admin")
            if not data.confirmed:
                raise Problem(422, "Explicit removal confirmation required")
            target = row(c, "SELECT role FROM admin_users WHERE id=?", (user_id,))
            if not target:
                raise Problem(404, "Staff account not found")
            auth(request).change_access(c, actor, user_id, data.expected_epoch, target["role"], False)
        return JSONResponse({"deactivated": True}) if wants_json(request) else RedirectResponse("/staff/users?removed=true", 303)

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
            auth(request).change_access(c, actor, user_id, data.expected_epoch, data.role, data.active)
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
