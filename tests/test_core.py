import pytest

from hh_auto_apply.apply import send_batch
from hh_auto_apply.claude_client import ClaudeClient
from hh_auto_apply.filters import check_vacancy
from hh_auto_apply.hh_client import CaptchaRequired, HHError
from hh_auto_apply.models import BatchItem, Vacancy
from hh_auto_apply.storage import Storage

F = {"stop_words": ["финансовый аналитик"], "include_roles": ["product manager"],
     "areas": ["160"], "allow_remote": True, "min_salary": 300000, "salary_currency": "KZT"}


def vac(**kw):
    d = dict(id="1", title="Бизнес-аналитик", area="Алматы", area_id="160", salary_from=400000,
             salary_currency="KZT", raw_description="описание")
    d.update(kw)
    return Vacancy(**d)


def test_filters():
    assert check_vacancy(vac(), F).passed
    assert not check_vacancy(vac(title="Финансовый аналитик"), F).passed
    assert check_vacancy(vac(title="Product Manager", raw_description="финансовый аналитик рядом"), F).passed
    assert not check_vacancy(vac(area="Астана", area_id="159"), F).passed
    assert check_vacancy(vac(area="Астана", area_id="159", remote=True), F).passed
    assert not check_vacancy(vac(salary_from=100000), F).passed
    r = check_vacancy(vac(salary_from=None), F)
    assert r.passed and "не указана" in r.notes


def test_storage_dedup_and_resume():
    s = Storage(":memory:")
    s.save_vacancy(vac())
    a = s.create_application("1", "r", "text")
    assert not s.is_applied("1")
    s.update_application(a.id, status="approved")
    assert s.is_applied("1")
    s.update_application(a.id, status="sent")
    assert s.get_application(a.id).sent_at
    assert s.stats()["sent"] == 1


class FakeHH:
    def __init__(self, script):
        self.script = list(script)

    def negotiate(self, *a):
        r = self.script.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def items(s, n):
    out = []
    for i in range(n):
        v = vac(id=str(i))
        s.save_vacancy(v)
        out.append(BatchItem(v, s.create_application(v.id, "r", "letter"), decision="approve"))
    return out


def test_send_captcha_then_continue():
    s = Storage(":memory:")
    its = items(s, 3)
    hh = FakeHH([{}, CaptchaRequired(403, "c", "u"), {}, HHError(400, "dup")])
    res = send_batch(hh, s, its, "r", delay=0, on_captcha=lambda it, url: True)
    assert (res.sent, res.errors, res.captchas) == (2, 1, 1)
    assert s.get_application(its[1].application.id).status == "sent"
    assert s.get_application(its[2].application.id).status == "error"


def test_send_captcha_abort_keeps_state():
    s = Storage(":memory:")
    its = items(s, 2)
    hh = FakeHH([CaptchaRequired(403, "c", "u")])
    res = send_batch(hh, s, its, "r", delay=0, on_captcha=lambda it, url: False)
    assert res.sent == 0 and len(res.remaining) == 2
    assert s.get_application(its[0].application.id).status == "captcha_pending"


class FakeMsgs:
    def __init__(self, outs): self.outs = list(outs)

    def create(self, **kw):
        class B: type = "text"; text = self.outs.pop(0)
        class R: content = [B()]
        return R()


class FakeAnthropic:
    def __init__(self, outs): self.messages = FakeMsgs(outs)


def test_letter_shortened_when_too_long():
    c = ClaudeClient("m", max_chars=100, client=FakeAnthropic(["x" * 500, "short"]))
    assert c.generate_letter("resume", vac()) == "short"


def test_outbox_flow(tmp_path, monkeypatch):
    from hh_auto_apply import outbox
    monkeypatch.setattr(outbox, "OUTBOX", tmp_path)
    s = Storage(":memory:")
    its = items(s, 2)
    outbox.save(its)
    for it in its:
        s.update_application(it.application.id, status="approved")
    assert len(outbox.show_pending(s)) == 2
    assert outbox.mark_sent(s, ("0",), False) == 1
    assert s.is_applied_sent("0") and not s.is_applied_sent("1")


def test_review_auto_approve():
    from click.testing import CliRunner
    import click
    from hh_auto_apply import batch_ui
    s = Storage(":memory:")
    its = items(s, 2)
    its[1].application.cover_letter_text = ""   # без письма — не одобряется

    @click.command()
    def cmd():
        res = batch_ui.review(its, s, lambda it: "x", auto_approve=True)
        click.echo(f"N={len(res)}")

    out = CliRunner().invoke(cmd, input="y\n").output
    assert "N=1" in out
