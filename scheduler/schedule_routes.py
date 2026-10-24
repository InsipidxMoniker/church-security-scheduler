from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from pydantic import Field, BaseModel, ConfigDict
from starlette.concurrency import run_in_threadpool

from scheduler.app import STAFF, Form, form, wants_json


class GenerateForm(Form):
    draft_id: str = Field(default="", max_length=36)
    draft_revision: int = Field(ge=0)
    date_input_epoch: int = Field(ge=1)
    roster_epoch: int = Field(ge=1)
    config_epoch: int = Field(ge=1)


EPOCH_FIELDS = {"draft_id", "draft_revision", "date_input_epoch", "roster_epoch", "config_epoch"}


class Change(BaseModel):
    model_config = ConfigDict(extra="forbid")
    service_number: int = Field(ge=1, le=2)
    position_number: int = Field(ge=1, le=8)
    volunteer_id: str | None = Field(default=None, max_length=36)
    locked: bool = False


class EditForm(GenerateForm):
    changes: list[Change] = Field(min_length=1, max_length=16)
    rotation_reasons: dict[str, str] = Field(default_factory=dict)


class SlotForm(GenerateForm):
    service_number: int = Field(ge=1, le=2)
    position_number: int = Field(ge=1, le=8)
    volunteer_id: str = Field(default="", max_length=36)
    locked: bool
    rotation_reason: str = Field(default="", max_length=1000)


class SwapForm(GenerateForm):
    first_slot: str = Field(max_length=3)
    second_slot: str = Field(max_length=3)
    rotation_volunteer_id: str = Field(default="", max_length=36)
    rotation_reason: str = Field(default="", max_length=1000)


class VacancyForm(GenerateForm):
    slot_keys: str = Field(min_length=3, max_length=100)
    reason: str = Field(min_length=1, max_length=1000)


class PublishForm(GenerateForm):
    idempotency_key: str = Field(min_length=1, max_length=100)
    confirmed: bool = False


class AmendForm(Form):
    base_version_id: str = Field(min_length=36, max_length=36)


def parse_keys(value):
    from scheduler.security import Problem
    try:
        keys = [tuple(map(int, part.strip().split(":"))) for part in value.split(",")]
        if any(len(k) != 2 or k[0] not in (1, 2) or k[1] not in range(1, 9) for k in keys):
            raise ValueError()
        return keys
    except ValueError:
        raise Problem(422, "Identify slots as service:position, separated by commas") from None


def register_schedules(app, view):
    @app.get("/staff/dates/{date_id}/draft")
    async def review(date_id: str, request: Request):
        from scheduler.security import Problem
        try:
            page_number = int(request.query_params.get("page", "1"))
        except ValueError:
            raise Problem(422, "Invalid volunteer page") from None
        data = await run_in_threadpool(app.state.publication.review, request.cookies.get(STAFF), date_id)
        roster = await run_in_threadpool(app.state.flow.roster, request.cookies.get(STAFF), page_number, 50, request.query_params.get("q", ""), "active")
        data["candidates"] = roster
        return JSONResponse(data) if wants_json(request) else view(request, "draft.html", **data)

    def expected(data):
        return data.model_dump(include=EPOCH_FIELDS)

    def done(request, date_id, result):
        return JSONResponse(result) if wants_json(request) else RedirectResponse(f"/staff/dates/{date_id}/draft", 303)

    @app.post("/staff/dates/{date_id}/draft/edit")
    async def edit(date_id: str, request: Request):
        data = await form(request, EditForm)
        result = await run_in_threadpool(app.state.publication.edit, request.cookies.get(STAFF), data.csrf, date_id, expected(data), [c.model_dump() for c in data.changes], data.rotation_reasons)
        return done(request, date_id, result)

    @app.post("/staff/dates/{date_id}/draft/slot")
    async def slot(date_id: str, request: Request):
        data = await form(request, SlotForm)
        change = {"service_number": data.service_number, "position_number": data.position_number, "volunteer_id": data.volunteer_id or None, "locked": data.locked}
        reasons = {data.volunteer_id: data.rotation_reason} if data.rotation_reason.strip() else {}
        return done(request, date_id, await run_in_threadpool(app.state.publication.edit, request.cookies.get(STAFF), data.csrf, date_id, expected(data), [change], reasons))

    @app.post("/staff/dates/{date_id}/draft/swap")
    async def swap(date_id: str, request: Request):
        data = await form(request, SwapForm)
        # Service rechecks the exact revision after the read, within its write transaction.
        review = await run_in_threadpool(app.state.publication.review, request.cookies.get(STAFF), date_id)
        slots = {(s["service_number"], s["position_number"]): s for s in review["slots"]}
        a, b = parse_keys(data.first_slot + "," + data.second_slot)
        from scheduler.security import Problem
        if a == b or a not in slots or b not in slots:
            raise Problem(422, "Choose two distinct draft slots")
        changes = [{"service_number": k[0], "position_number": k[1], "volunteer_id": slots[other]["volunteer_id"], "locked": bool(slots[k]["locked"])} for k, other in ((a,b), (b,a))]
        reasons = {data.rotation_volunteer_id: data.rotation_reason} if data.rotation_reason.strip() else {}
        return done(request, date_id, await run_in_threadpool(app.state.publication.edit, request.cookies.get(STAFF), data.csrf, date_id, expected(data), changes, reasons))

    @app.post("/staff/dates/{date_id}/draft/vacancies")
    async def vacancies(date_id: str, request: Request):
        data = await form(request, VacancyForm)
        return done(request, date_id, await run_in_threadpool(app.state.publication.approve_vacancies, request.cookies.get(STAFF), data.csrf, date_id, expected(data), parse_keys(data.slot_keys), data.reason))

    @app.post("/staff/dates/{date_id}/draft/refresh")
    async def refresh(date_id: str, request: Request):
        data = await form(request, GenerateForm)
        return done(request, date_id, await run_in_threadpool(app.state.publication.refresh, request.cookies.get(STAFF), data.csrf, date_id, expected(data)))

    @app.post("/staff/dates/{date_id}/draft/publish")
    async def publish(date_id: str, request: Request):
        data = await form(request, PublishForm)
        result = await run_in_threadpool(app.state.publication.publish, request.cookies.get(STAFF), data.csrf, date_id, expected(data), data.idempotency_key, data.confirmed)
        return JSONResponse(result) if wants_json(request) else RedirectResponse(f"/staff/dates/{date_id}/publications/{result['version_id']}", 303)

    @app.post("/staff/dates/{date_id}/amend")
    async def amend(date_id: str, request: Request):
        data = await form(request, AmendForm)
        return done(request, date_id, await run_in_threadpool(app.state.publication.amend, request.cookies.get(STAFF), data.csrf, date_id, data.base_version_id))

    @app.get("/staff/dates/{date_id}/publications/{version_id}")
    async def historical(date_id: str, version_id: str, request: Request):
        from scheduler.exports import signal_text
        data = await run_in_threadpool(app.state.publication.historical, request.cookies.get(STAFF), date_id, version_id)
        data["signal_text"] = signal_text(data["payload"], data["current"])
        return JSONResponse(data) if wants_json(request) else view(request, "publication.html", **data)

    @app.get("/staff/dates/{date_id}/publications/{version_id}/signal.txt")
    async def signal(date_id: str, version_id: str, request: Request):
        from scheduler.exports import signal_text
        data = await run_in_threadpool(app.state.publication.historical, request.cookies.get(STAFF), date_id, version_id)
        return Response(signal_text(data["payload"], data["current"]), media_type="text/plain; charset=utf-8")

    @app.get("/staff/dates/{date_id}/publications/{version_id}/schedule.pdf")
    async def pdf(date_id: str, version_id: str, request: Request):
        from scheduler.exports import bounded_pdf
        data = await run_in_threadpool(app.state.publication.historical, request.cookies.get(STAFF), date_id, version_id)
        content = await run_in_threadpool(bounded_pdf, data["payload"], data["current"])
        filename = f"security-{data['payload']['local_date']}-v{data['payload']['publication_number']}.pdf"
        return Response(content, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    @app.post("/staff/dates/{date_id}/draft/generate")
    async def generate(date_id: str, request: Request):
        data = await form(request, GenerateForm)
        result = await app.state.generation.generate(request.cookies.get(STAFF), data.csrf, date_id,
                                                     data.model_dump(exclude={"csrf"}))
        return JSONResponse(result) if wants_json(request) else RedirectResponse(f"/staff/dates/{date_id}/draft", 303)
