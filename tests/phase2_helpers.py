from fastapi.testclient import TestClient

from scheduler.phase2_routes import VOLUNTEER_CSRF
from conftest import post, sign_in


def settings_payload(revision=1, **changes):
    return {"expected_revision": revision, "timezone": "America/Los_Angeles", "service_1_label": "First service", "service_2_label": "Second service", "service_1_time": "", "service_2_time": "", "essentiality_confirmed": True, "pin_digits": 8, "link_days": 30, **{f"essential_{i}": True for i in range(1, 9)}, **changes}


def create_person(client, name="Synthetic Alex", **changes):
    result = post(client, "/staff/volunteers", **{"display_name":name, "public_label":"", "active":True, "restricted_qualified":False, **changes})
    assert result.status_code == 200, result.text
    return result.json()


def open_date(client, day="2026-10-25"):
    result = post(client, "/staff/dates", local_date=day)
    assert result.status_code == 200, result.text
    return result.json()


def new_link(client, day, operation="create"):
    result = post(client, f"/staff/dates/{day['id']}/link", expected_epoch=day["date_input_epoch"], operation=operation)
    assert result.status_code == 200, result.text
    return result.json()


def verify(client, link, volunteer_id, pin):
    public = client.get(f"/availability/{link}", headers={"Accept": "application/json"})
    assert public.status_code == 200, public.text
    return client.post(f"/availability/{link}/verify-pin", json={"csrf": public.json()["csrf"], "volunteer_id": volunteer_id, "pin": pin}, headers={"Origin": "https://testserver"})


def save(client, link, revision=0, first=True, second=False, **extra):
    return client.put(f"/availability/{link}/mine", json={"csrf": client.cookies.get(VOLUNTEER_CSRF, ""), "expected_revision": revision, "service_1_available": first, "service_2_available": second, **extra}, headers={"Origin": "https://testserver"})


def detail(client, path):
    result = client.get(path, headers={"Accept": "application/json"})
    assert result.status_code == 200, result.text
    return result.json()


def setup(world):
    sign_in(world)
    result = post(world.client, "/staff/settings", **settings_payload())
    assert result.status_code == 200, result.text
    person = create_person(world.client)
    result = post(world.client, f"/staff/volunteers/{person['id']}/pin", expected_revision=person["revision"])
    assert result.status_code == 200, result.text
    person["revision"] = result.json()["revision"]
    pin = result.json()["pin"]
    day = open_date(world.client)
    link = new_link(world.client, day)
    day["date_input_epoch"] = link["date_input_epoch"]
    client = TestClient(world.app, base_url="https://testserver")
    assert verify(client, link["token"], person["id"], pin).status_code == 200
    return person, pin, day, link["token"], client
