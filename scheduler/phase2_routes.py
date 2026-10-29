"""HTML and JSON adapters for configuration, roster and availability."""
from typing import Literal

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import Field, SecretStr, ValidationError
from starlette.concurrency import run_in_threadpool

from scheduler.app import CSRF, STAFF, Form, cookie, clear_cookie, form, wants_json
from scheduler.security import Problem
from scheduler.volunteer_flow import CHOICES

VOLUNTEER = "__Host-volunteer"
VOLUNTEER_CSRF = "__Host-volunteer-csrf"
VOLUNTEER_PREAUTH = "__Host-volunteer-preauth"


class SettingsForm(Form):
    expected_revision: int = Field(ge=1)
    timezone: str = Field(min_length=1, max_length=100)
    service_1_label: str = Field(min_length=1, max_length=100)
    service_2_label: str = Field(min_length=1, max_length=100)
    inside_zone_label: str | None = Field(default=None, max_length=100)
    outside_zone_label: str | None = Field(default=None, max_length=100)
    service_1_time: str | None = Field(default=None, max_length=5)
    service_2_time: str | None = Field(default=None, max_length=5)
    essential_1: bool
    essential_2: bool
    essential_3: bool
    essential_4: bool
    essential_5: bool
    essential_6: bool
    essential_7: bool
    essential_8: bool
    essentiality_confirmed: bool
    pin_digits: int = Field(default=8, ge=4, le=12)
    link_days: float = Field(default=30, gt=0)


class PersonForm(Form):
    display_name: str = Field(min_length=1, max_length=200)
    public_label: str = Field(default="", max_length=200)
    active: bool
    outside_available: bool = False
    rifle_qualified: bool = False


class PersonEdit(PersonForm):
    expected_revision: int = Field(ge=1)
    outside_available: bool
    rifle_qualified: bool | None = None


class Revision(Form):
    expected_revision: int = Field(ge=1)


class DeleteVolunteer(Revision):
    confirmed: bool


class PINOptions(Revision):
    mode: Literal["random", "custom"] = "random"
    custom_pin: SecretStr = SecretStr("")


class CSVPreview(Form):
    csv_text: str = Field(min_length=1)


class CSVCommit(Form):
    preview: str = Field(min_length=1)


class Sunday(Form):
    local_date: str = Field(min_length=10, max_length=10)


class Collection(Form):
    expected_epoch: int = Field(ge=1)
    opened: bool


class Link(Form):
    expected_epoch: int = Field(ge=1)
    operation: Literal["create", "rotate", "revoke"]


class Verify(Form):
    volunteer_id: str = Field(min_length=1, max_length=36)
    pin: str = Field(min_length=1, max_length=128)


class Response(Form):
    expected_revision: int = Field(ge=0)
    service_1_available: bool
    service_2_available: bool


class Choice(Form):
    expected_revision: int = Field(ge=0)
    choice: Literal["first", "second", "both", "neither"]


class ConfirmedResponse(Response):
    confirmation_reason: str = Field(min_length=1, max_length=1000)


async def csv_upload(request, limit):
    # FormData owns all spooled files, including malformed/unauthorized forms.
    # Its context closes them on validation failure as well as success.
    async with request.form() as inputs:
        values = dict(inputs)
        if set(values) - {"csrf", "csv_text", "csv_file"}:
            raise Problem(422, "Invalid CSV upload fields")
        try:
            csrf = Form.model_validate({"csrf": values.get("csrf")}).csrf
        except ValidationError:
            raise Problem(422, "CSV preview requires a CSRF token") from None
        text = values.get("csv_text", "")
        if not isinstance(text, str):
            raise Problem(422, "Pasted CSV must be text")
        upload = values.get("csv_file")
        if upload and getattr(upload, "filename", ""):
            if text.strip():
                raise Problem(422, "Choose either a file or pasted CSV")
            try:
                text = (await upload.read(limit + 1)).decode("utf-8")
            except UnicodeError:
                raise Problem(422, "CSV must be UTF-8 text") from None
        return csrf, text


def register_phase2(app, origin, client_ip, view):
    def flow(request):
        return request.app.state.flow

    def raw(request):
        return request.cookies.get(STAFF)

    def result(request, data, destination):
        return JSONResponse(data) if wants_json(request) else RedirectResponse(destination, 303)

    @app.get("/staff/settings")
    async def settings(request: Request):
        data = await run_in_threadpool(flow(request).read_settings, raw(request))
        return JSONResponse(data) if wants_json(request) else view(request, "settings.html", settings=data)

    @app.post("/staff/settings")
    async def settings_save(request: Request):
        data = await form(request, SettingsForm)
        saved = await run_in_threadpool(flow(request).save_settings, raw(request), data)
        return result(request, saved, "/staff/settings")

    @app.get("/staff/volunteers")
    async def roster(request: Request, page: int = 1, page_size: int = 50, q: str = "", status: str = "active", removed: bool = False):
        data = await run_in_threadpool(flow(request).roster, raw(request), page, page_size, q, status)
        return JSONResponse(data) if wants_json(request) else view(request, "roster.html", removed=removed, **data)

    @app.get("/staff/volunteers/new")
    async def new_person(request: Request):
        # Authorize before returning any staff form.
        await run_in_threadpool(flow(request).roster, raw(request), 1, 1)
        return view(request, "person.html", person=None, role=None)

    @app.post("/staff/volunteers")
    async def create_person(request: Request):
        data = await form(request, PersonForm)
        saved = await run_in_threadpool(flow(request).save_person, raw(request), data)
        return result(request, saved, "/staff/volunteers/" + saved["id"])

    @app.get("/staff/volunteers/import")
    async def csv_page(request: Request):
        await run_in_threadpool(flow(request).roster, raw(request), 1, 1)
        return view(request, "csv_import.html", preview=None, csv_limit=flow(request).csv_max_bytes)

    @app.post("/staff/volunteers/import/preview")
    async def csv_preview(request: Request):
        if request.headers.get("content-type", "").startswith("multipart/form-data"):
            csrf, text = await csv_upload(request, flow(request).csv_max_bytes)
        else:
            data = await form(request, CSVPreview)
            csrf, text = data.csrf, data.csv_text
        data = await run_in_threadpool(flow(request).preview_csv, raw(request), csrf, text)
        return JSONResponse(data) if wants_json(request) else view(request, "csv_import.html", preview=data, csv_limit=flow(request).csv_max_bytes)

    @app.post("/staff/volunteers/import/commit")
    async def csv_commit(request: Request):
        data = await form(request, CSVCommit)
        saved = await run_in_threadpool(flow(request).commit_csv, raw(request), data.csrf, data.preview)
        return result(request, saved, "/staff/volunteers")

    @app.get("/staff/volunteers/{volunteer_id}")
    async def person(volunteer_id: str, request: Request):
        data = await run_in_threadpool(flow(request).read_person, raw(request), volunteer_id)
        return JSONResponse(data) if wants_json(request) else view(request, "person.html", **data)

    @app.post("/staff/volunteers/{volunteer_id}")
    async def edit_person(volunteer_id: str, request: Request):
        data = await form(request, PersonEdit)
        saved = await run_in_threadpool(flow(request).save_person, raw(request), data, volunteer_id)
        return result(request, saved, "/staff/volunteers/" + volunteer_id)

    @app.get("/staff/volunteers/{volunteer_id}/delete")
    async def confirm_volunteer_removal(volunteer_id: str, request: Request):
        data = await run_in_threadpool(flow(request).read_person, raw(request), volunteer_id)
        person = data["person"]
        return view(request, "confirm_removal.html", name=person["display_name"] + " (" + person["public_label"] + ")", kind="volunteer", action=f"/staff/volunteers/{volunteer_id}/delete", cancel="/staff/volunteers/" + volunteer_id, revision_field="expected_revision", revision=person["revision"])

    @app.post("/staff/volunteers/{volunteer_id}/delete")
    async def delete_volunteer(volunteer_id: str, request: Request):
        data = await form(request, DeleteVolunteer)
        saved = await run_in_threadpool(flow(request).save_person, raw(request), data, volunteer_id, True)
        return result(request, saved, "/staff/volunteers?removed=true")

    @app.post("/staff/volunteers/{volunteer_id}/pin")
    async def pin(volunteer_id: str, request: Request):
        data = await form(request, PINOptions)
        saved = await run_in_threadpool(flow(request).issue_pin, raw(request), data.csrf, volunteer_id, data.expected_revision, data.mode, data.custom_pin.get_secret_value())
        return JSONResponse(saved) if wants_json(request) else view(request, "pin_once.html", person_id=volunteer_id, viewing=False, **saved)

    @app.post("/staff/volunteers/{volunteer_id}/pin/view")
    async def view_pin(volunteer_id: str, request: Request):
        data = await form(request, Revision)
        saved = await run_in_threadpool(flow(request).view_pin, raw(request), data.csrf, volunteer_id, data.expected_revision)
        return JSONResponse(saved) if wants_json(request) else view(request, "pin_once.html", person_id=volunteer_id, viewing=True, **saved)

    @app.post("/staff/volunteers/{volunteer_id}/clear-block")
    async def clear_block(volunteer_id: str, request: Request):
        data = await form(request, Form)
        saved = await run_in_threadpool(flow(request).clear_pin_block, raw(request), data.csrf, volunteer_id)
        return result(request, saved, "/staff/volunteers/" + volunteer_id)

    @app.get("/staff/dates")
    async def sundays(request: Request, page: int = 1):
        data = await run_in_threadpool(flow(request).dates, raw(request), page)
        return JSONResponse(data) if wants_json(request) else view(request, "dates.html", **data)

    @app.post("/staff/dates")
    async def open_sunday(request: Request):
        data = await form(request, Sunday)
        saved = await run_in_threadpool(flow(request).open_date, raw(request), data.csrf, data.local_date)
        return result(request, saved, "/staff/dates/" + saved["id"])

    @app.get("/staff/dates/{date_id}")
    async def date_dashboard(date_id: str, request: Request, page: int = 1):
        data = await run_in_threadpool(flow(request).dashboard, raw(request), date_id, page)
        current_token = data.pop("link_token")
        data["shared_link_url"] = origin + "/availability/" + current_token if current_token else None
        return JSONResponse(data) if wants_json(request) else view(request, "date_dashboard.html", **data)

    @app.post("/staff/dates/{date_id}/collection")
    async def collection(date_id: str, request: Request):
        data = await form(request, Collection)
        saved = await run_in_threadpool(flow(request).collection, raw(request), data.csrf, date_id, data.expected_epoch, data.opened)
        return result(request, saved, "/staff/dates/" + date_id)

    @app.post("/staff/dates/{date_id}/link")
    async def link(date_id: str, request: Request):
        data = await form(request, Link)
        saved = await run_in_threadpool(flow(request).manage_link, raw(request), data.csrf, date_id, data.expected_epoch, data.operation)
        if "token" in saved:
            saved["url"] = origin + "/availability/" + saved["token"]
            return JSONResponse(saved) if wants_json(request) else view(request, "link_once.html", date_id=date_id, **saved)
        return result(request, saved, "/staff/dates/" + date_id)

    @app.post("/staff/dates/{date_id}/availability/{volunteer_id}")
    async def staff_availability(date_id: str, volunteer_id: str, request: Request):
        data = await form(request, ConfirmedResponse)
        saved = await run_in_threadpool(flow(request).staff_response, raw(request), data.csrf, date_id, volunteer_id, data.expected_revision, data.service_1_available, data.service_2_available, data.confirmation_reason)
        return result(request, saved, "/staff/dates/" + date_id)

    @app.get("/availability/{link_token}")
    async def public_link(link_token: str, request: Request, page: int = 1, page_size: int = 50, q: str = ""):
        data = await run_in_threadpool(flow(request).public_roster, link_token, client_ip(request), page, page_size, q)
        response = JSONResponse(data) if wants_json(request) else view(request, "volunteer_verify.html", link_token=link_token, **data)
        cookie(response, VOLUNTEER_PREAUTH, data["csrf"], 600)
        return response

    @app.post("/availability/{link_token}/verify-pin")
    async def verify_pin(link_token: str, request: Request):
        data = await form(request, Verify)
        session, csrf = await run_in_threadpool(flow(request).verify_pin, link_token, data.csrf, request.cookies.get(VOLUNTEER_PREAUTH), data.volunteer_id, data.pin, client_ip(request))
        response = result(request, {"next": "/availability/" + link_token + "/mine"}, "/availability/" + link_token + "/mine")
        cookie(response, VOLUNTEER, session, 7200)
        cookie(response, VOLUNTEER_CSRF, csrf, 7200)
        clear_cookie(response, VOLUNTEER_PREAUTH)
        return response

    @app.get("/availability/{link_token}/mine")
    async def mine(link_token: str, request: Request):
        if request.query_params:
            raise Problem(422, "Own-response requests do not accept another identity or date")
        data = await run_in_threadpool(flow(request).mine, link_token, request.cookies.get(VOLUNTEER))
        return JSONResponse(data) if wants_json(request) else view(request, "volunteer_response.html", link_token=link_token, csrf=request.cookies.get(VOLUNTEER_CSRF, ""), saved=False, **data)

    @app.put("/availability/{link_token}/mine")
    async def save_mine_api(link_token: str, request: Request):
        data = await form(request, Response)
        saved = await run_in_threadpool(flow(request).save_mine, link_token, request.cookies.get(VOLUNTEER), data.csrf, data.expected_revision, data.service_1_available, data.service_2_available)
        return JSONResponse(saved)

    @app.post("/availability/{link_token}/mine")
    async def save_mine_form(link_token: str, request: Request):
        data = await form(request, Choice)
        first, second = CHOICES[data.choice]
        saved = await run_in_threadpool(flow(request).save_mine, link_token, request.cookies.get(VOLUNTEER), data.csrf, data.expected_revision, first, second, True)
        if wants_json(request):
            return JSONResponse(saved["response"])
        return view(request, "volunteer_response.html", link_token=link_token, csrf=request.cookies.get(VOLUNTEER_CSRF, ""), saved=True, **saved)
