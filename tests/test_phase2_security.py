import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

from scheduler.app import CSRF
from scheduler.phase2_routes import VOLUNTEER, VOLUNTEER_CSRF
from scheduler.db import row
from conftest import post, sign_in
from phase2_helpers import create_person, detail, open_date, new_link, save, settings_payload, setup, verify


@pytest.mark.parametrize("path", ["/staff/settings","/staff/volunteers","/staff/volunteers/new","/staff/volunteers/import","/staff/dates"])
def test_new_staff_reads_unauthenticated(world,path):
    response=world.client.get(path,headers={"Accept":"application/json"})
    assert response.status_code==401


def test_mutations_require_csrf_origin_and_authorized_actor(world):
    person,_,day,_,_=setup(world)
    preview=post(world.client,"/staff/volunteers/import/preview",csv_text="display_name,restricted_qualified\nOther,true").json()["preview"]
    cases=[
        ("/staff/settings",settings_payload(2)),
        ("/staff/volunteers",{"display_name":"Other","active":True,"restricted_qualified":False}),
        (f"/staff/volunteers/{person['id']}",{"expected_revision":person["revision"],"display_name":"Changed","active":True,"restricted_qualified":True}),
        (f"/staff/volunteers/{person['id']}/pin",{"expected_revision":person["revision"]}),
        (f"/staff/volunteers/{person['id']}/clear-block",{}),
        ("/staff/volunteers/import/preview",{"csv_text":"display_name,restricted_qualified\nOther,true"}),
        ("/staff/volunteers/import/commit",{"preview":preview}),
        ("/staff/dates",{"local_date":"2026-11-01"}),
        (f"/staff/dates/{day['id']}/collection",{"expected_epoch":day["date_input_epoch"],"opened":False}),
        (f"/staff/dates/{day['id']}/link",{"expected_epoch":day["date_input_epoch"],"operation":"rotate"}),
        (f"/staff/dates/{day['id']}/availability/{person['id']}",{"expected_revision":0,"service_1_available":True,"service_2_available":True,"confirmation_reason":"Confirmed"}),
    ]
    anonymous=TestClient(world.app,base_url="https://testserver")
    for url,values in cases:
        assert post(world.client,url,csrf="forged",**values).status_code==403
        assert world.client.post(url,json={"csrf":world.client.cookies[CSRF],**values},headers={"Origin":"https://evil.example"}).status_code==403
        assert post(anonymous,url,csrf="forged",**values).status_code==401
    with world.db.engine.connect() as c:
        assert c.exec_driver_sql("SELECT count(*) FROM availability").scalar()==0
        assert row(c,"SELECT * FROM volunteers")["revision"]==person["revision"]


def test_pin_reset_clears_block_and_admin_can_clear_with_audit(world):
    person,pin,_,link,client=setup(world)
    for _ in range(5):
        assert verify(client,link,person["id"],"wrong").status_code==401
    assert verify(client,link,person["id"],pin).status_code==429
    assert post(world.client,f"/staff/volunteers/{person['id']}/clear-block").status_code==200
    assert verify(client,link,person["id"],pin).status_code==200
    for _ in range(5):
        assert verify(client,link,person["id"],"wrong").status_code==401
    reset=post(world.client,f"/staff/volunteers/{person['id']}/pin",expected_revision=person["revision"])
    assert reset.status_code==200
    assert verify(client,link,person["id"],reset.json()["pin"]).status_code==200
    with world.db.engine.connect() as c:
        assert c.exec_driver_sql("SELECT count(*) FROM audit_events WHERE action='volunteer.block_cleared'").scalar()==1


def test_concurrent_wrong_pin_guesses_are_atomic(world):
    person,_,_,link,_=setup(world)
    clients=[TestClient(world.app,base_url="https://testserver") for _ in range(8)]
    payloads=[]
    for client in clients:
        public=detail(client,f"/availability/{link}")
        payloads.append({"csrf":public["csrf"],"volunteer_id":person["id"],"pin":"wrong"})
    # Hold no SQLite transaction during hashing; two-hash bound remains active.
    start=threading.Barrier(8)
    def guess(index):
        start.wait(timeout=5)
        return clients[index].post(f"/availability/{link}/verify-pin",json=payloads[index],headers={"Origin":"https://testserver"}).status_code
    with ThreadPoolExecutor(8) as pool:
        statuses=list(pool.map(guess,range(8)))
    assert all(code in (401,429) for code in statuses)
    assert statuses.count(401)<=5 and statuses.count(429)>=3
    from scheduler.security import digest
    with world.db.engine.connect() as c:
        throttle=row(c,"SELECT * FROM auth_throttles WHERE scope_hash=?",(digest("volunteer:"+person["id"]),))
        assert throttle["count"]==statuses.count(401)<=5
        if throttle["count"]==5:
            assert throttle["blocked_until"]>world.now[0]


def test_verify_cannot_issue_session_after_racing_pin_reset(world,monkeypatch):
    person,pin,_,link,_=setup(world)
    client=TestClient(world.app,base_url="https://testserver")
    public=detail(client,f"/availability/{link}")
    held,release=threading.Event(),threading.Event()
    original=world.security.verify
    def slow(*args):
        held.set()
        assert release.wait(5)
        return original(*args)
    monkeypatch.setattr(world.security,"verify",slow)
    with ThreadPoolExecutor(1) as pool:
        pending=pool.submit(client.post,f"/availability/{link}/verify-pin",json={"csrf":public["csrf"],"volunteer_id":person["id"],"pin":pin},headers={"Origin":"https://testserver"})
        assert held.wait(5)
        reset=post(world.client,f"/staff/volunteers/{person['id']}/pin",expected_revision=person["revision"])
        assert reset.status_code==200
        release.set()
        assert pending.result().status_code==401
    with world.db.engine.connect() as c:
        assert c.exec_driver_sql("SELECT count(*) FROM volunteer_sessions WHERE revoked_at IS NULL").scalar()==0


def test_pin_reset_rejects_concurrent_roster_edit_after_hashing(world,monkeypatch):
    person,_,_,_,_=setup(world)
    held,release=threading.Event(),threading.Event()
    original=world.security.hash_password
    def slow(*args):
        held.set()
        assert release.wait(5)
        return original(*args)
    monkeypatch.setattr(world.security,"hash_password",slow)
    with ThreadPoolExecutor(1) as pool:
        pending=pool.submit(post,world.client,f"/staff/volunteers/{person['id']}/pin",expected_revision=person["revision"])
        assert held.wait(5)
        changed=post(world.client,f"/staff/volunteers/{person['id']}",expected_revision=person["revision"],display_name="New confirmed name",public_label="",active=True,restricted_qualified=True)
        assert changed.status_code==200
        release.set()
        assert pending.result().status_code==409
    with world.db.engine.connect() as c:
        stored=row(c,"SELECT * FROM volunteers WHERE id=?",(person["id"],))
        assert stored["display_name"]=="New confirmed name" and stored["revision"]==person["revision"]+1
        assert c.exec_driver_sql("SELECT count(*) FROM audit_events WHERE action='volunteer.pin_reset'").scalar()==0


def test_pin_and_link_mutations_rollback_if_audit_fails(world):
    person,pin,day,link,client=setup(world)
    with world.db.transaction() as c:
        c.exec_driver_sql("CREATE TRIGGER stop_secrets BEFORE INSERT ON audit_events WHEN NEW.action IN ('volunteer.pin_reset','date.link_rotate') BEGIN SELECT RAISE(ABORT,'injected'); END")
    assert post(world.client,f"/staff/volunteers/{person['id']}/pin",expected_revision=person["revision"]).status_code==409
    assert post(world.client,f"/staff/dates/{day['id']}/link",expected_epoch=day["date_input_epoch"],operation="rotate").status_code==409
    assert verify(client,link,person["id"],pin).status_code==200
    with world.db.engine.connect() as c:
        assert row(c,"SELECT * FROM volunteers")["revision"]==person["revision"]
        assert row(c,"SELECT * FROM date_links")["revoked_at"] is None
        assert row(c,"SELECT * FROM service_dates")["date_input_epoch"]==day["date_input_epoch"]


def test_public_roster_paging_privacy_get_no_business_mutation_and_query_counts(world):
    _,_,day,link,client=setup(world)
    text="display_name,restricted_qualified\n"+"\n".join(f"Page {i:03d},false" for i in range(115))
    preview=post(world.client,"/staff/volunteers/import/preview",csv_text=text).json()["preview"]
    assert post(world.client,"/staff/volunteers/import/commit",preview=preview).status_code==200
    hidden=create_person(world.client,"Not public",active=False)
    pages=[detail(client,f"/availability/{link}?page={i}") for i in (1,2,3)]
    assert [len(p["volunteers"]) for p in pages]==[50,50,16]
    assert hidden["id"] not in str(pages)
    assert detail(client,f"/availability/{link}?q=114")["volunteers"][0]["public_label"]=="Page 114"
    assert client.get(f"/availability/{link}?page_size=101").status_code==422
    with world.db.engine.connect() as c:
        assert row(c,"SELECT * FROM service_dates")["date_input_epoch"]==day["date_input_epoch"]
        assert c.exec_driver_sql("SELECT count(*) FROM availability").scalar()==0
    queries=[]
    def count(c,cursor,statement,parameters,context,many):
        if statement.lstrip().upper().startswith("SELECT"):
            queries.append(statement)
    event.listen(world.db.engine,"before_cursor_execute",count)
    try:
        detail(world.client,f"/staff/dates/{day['id']}")
        assert len(queries)<=10
    finally:
        event.remove(world.db.engine,"before_cursor_execute",count)


def test_expired_preview_invalid_upload_and_security_only_settings_revision(world):
    person,_,_,_,_=setup(world)
    payload={"actor":world.admin_id,"text":"display_name,restricted_qualified\nExpired,false","epoch":1,"purpose":"roster-import"}
    expired=world.security.cipher.encrypt_at_time(json.dumps(payload).encode(),int(time.time())-901).decode()
    assert post(world.client,"/staff/volunteers/import/commit",preview=expired).status_code==409
    upload=world.client.post("/staff/volunteers/import/preview",files={"csv_file":("x.csv",b"display_name,restricted_qualified\nX,false")},headers={"Origin":"https://testserver","Accept":"application/json"})
    assert upload.status_code==422
    saved=post(world.client,"/staff/settings",**settings_payload(2,pin_digits=10,link_days=2)).json()
    assert saved["settings_revision"]==3 and saved["config_epoch"]==2
    pin=post(world.client,f"/staff/volunteers/{person['id']}/pin",expected_revision=person["revision"]).json()["pin"]
    assert len(pin)==10
    day=open_date(world.client,"2026-03-08")
    issued=new_link(world.client,day)
    assert issued["expires_at"]==world.now[0]+2*86400


def test_configurable_csv_and_request_bounds(world, monkeypatch):
    from scheduler.app import create_app
    monkeypatch.setenv("CSV_MAX_BYTES","200")
    monkeypatch.setenv("MAX_BODY_BYTES","8192")
    app=create_app(world.db,world.security,"https://testserver",lambda:world.now[0])
    with TestClient(app,base_url="https://testserver") as client:
        sign_in(world,client=client)
        assert post(client,"/staff/volunteers/import/preview",csv_text="x"*201).status_code==422
        assert client.post("/staff/volunteers/import/preview",content=b"x"*8193,headers={"Origin":"https://testserver","Accept":"application/json"}).status_code==413


def test_pin_reset_preserves_response(world):
    person,_,_,link,client=setup(world)
    assert save(client,link,first=True,second=True).status_code==200
    reset=post(world.client,f"/staff/volunteers/{person['id']}/pin",expected_revision=person["revision"])
    assert reset.status_code==200
    assert verify(client,link,person["id"],reset.json()["pin"]).status_code==200
    assert detail(client,f"/availability/{link}/mine")["response"]["choice"]=="both"
