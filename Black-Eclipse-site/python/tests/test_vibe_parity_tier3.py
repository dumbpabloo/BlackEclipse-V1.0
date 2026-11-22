"""Numeric parity, third tier: reconnect replay and the channel status surface.

References fetched 2026-11-21 (MIT, `HKUDS/Vibe-Trading`):

* ``agent/src/session/events.py::EventBus._replay_locked`` — reconnect replay by
  ``last_event_id``. Three branches, and all three matter: no id yields nothing
  unless ``replay_all``; a found id yields everything strictly after it; an id that
  is gone yields nothing unless ``replay_all`` (never a silent restart from the
  beginning, which is how a client shows duplicates). The buffer is bounded.
* ``agent/src/channels/registry.py::inspect_channels`` — the status surface unions
  the built-in channel names with the names present in the operator's config, so a
  configured-but-unimplemented channel is visible as unknown rather than absent.

Eclipse had the execution log and the provider specs, but no replay-by-id and no
configured-but-unknown view; both are ported here and pinned against a
reimplementation of the reference.
"""

from __future__ import annotations

import pytest

from harness.channels import registry as R
from harness.execution_log import replay_after


# -- the reference replay, reimplemented from source ----------------------


def reference_replay(events, last_event_id=None, *, replay_all=False):
    if not last_event_id:
        return list(events) if replay_all else []
    found = False
    result = []
    for event in events:
        if found:
            result.append(event)
        elif event["id"] == last_event_id:
            found = True
    if not found and replay_all:
        return list(events)
    return result


EVENTS = [{"id": f"e{n}", "seq": n} for n in range(6)]

FIXTURES = [
    (None, False),
    (None, True),
    ("e0", False),
    ("e3", False),
    ("e5", False),
    ("e9", False),
    ("e9", True),
]


@pytest.mark.parametrize("last_id,replay_all", FIXTURES)
def test_replay_matches_the_reference(last_id, replay_all):
    assert replay_after(EVENTS, last_id, replay_all=replay_all) == reference_replay(
        EVENTS, last_id, replay_all=replay_all
    )


def test_the_last_event_replays_nothing():
    assert replay_after(EVENTS, "e5") == []


def test_an_unknown_id_does_not_silently_restart_the_stream():
    """The whole point: a client that cannot resume must not get a duplicate flood."""
    assert replay_after(EVENTS, "evicted") == []
    assert replay_after(EVENTS, "evicted", replay_all=True) == EVENTS


def test_a_first_connect_gets_nothing_unless_it_asks():
    assert replay_after(EVENTS) == []
    assert replay_after(EVENTS, None, replay_all=True) == EVENTS


def test_replay_preserves_order():
    assert [e["seq"] for e in replay_after(EVENTS, "e2")] == [3, 4, 5]


# -- the channel status surface ------------------------------------------


def test_every_built_in_provider_is_described():
    described = R.describe()
    assert set(described) == set(R.PROVIDERS)
    for name, row in described.items():
        assert row["known"] is True
        assert row["configured"] is False
        assert row["kind"] == name


def test_a_configured_unknown_channel_is_visible_not_dropped():
    described = R.describe(["telegram", "carrier_pigeon"])
    assert described["telegram"]["configured"] is True
    assert described["telegram"]["known"] is True
    # the name this build does not implement is still reported, flagged unknown
    assert described["carrier_pigeon"]["known"] is False
    assert described["carrier_pigeon"]["configured"] is True


def test_describe_is_sorted_and_stable():
    names = list(R.describe(["zzz", "aaa"]))
    assert names == sorted(names)
    assert names == list(R.describe(["zzz", "aaa"]))


def test_an_outbound_only_provider_is_in_neither_inbound_set():
    """The registry's docstring used to claim a three-way partition; it is not one.

    A `both` provider (lark) is legitimately in PUSH_KINDS and POLL_KINDS. The
    invariant that does hold: outbound-only means in neither, and everything else is
    in at least one.
    """
    described = R.describe()
    for name in described:
        in_push = name in R.PUSH_KINDS
        in_poll = name in R.POLL_KINDS
        outbound = name in R.OUTBOUND_KINDS
        if outbound:
            assert not in_push and not in_poll, name
        else:
            assert in_push or in_poll, name


def test_a_both_mode_provider_is_in_both_inbound_sets():
    both = [name for name, spec in R.PROVIDERS.items() if spec.inbound_mode == "both"]
    assert both, "the fixture assumes at least one 'both' provider exists"
    for name in both:
        assert name in R.PUSH_KINDS and name in R.POLL_KINDS


def test_the_nineteen_channel_families_are_all_present():
    # the reference's 19 platform families, as documented in the analysis
    expected = {
        "telegram", "discord", "email", "slack", "lark", "teams", "google_chat",
        "whatsapp", "webhook", "dingtalk", "mattermost", "matrix", "zulip",
        "rocketchat", "signal", "twilio", "viber", "line", "wecom",
    }
    assert expected <= set(R.describe())
    assert len(R.PROVIDERS) >= 19
