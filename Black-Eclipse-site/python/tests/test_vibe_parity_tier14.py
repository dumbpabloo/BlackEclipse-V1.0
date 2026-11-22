"""Branch parity, fourteenth tier: the model-endpoint guard.

Reference fetched 2026-11-21 (MIT, `HKUDS/Vibe-Trading`,
`agent/src/providers/endpoint_guard.py`).

Two findings, both real:

* **Eclipse ALLOWED `100.100.100.200`** — Alibaba Cloud's metadata service, in the
  shared address space `100.64.0.0/10`, which ``is_private`` does *not* cover. A
  user-supplied OpenAI-compatible endpoint could therefore reach cloud metadata
  credentials. The reference says it plainly: ``is_private`` misses it, so judge by
  ``is_global``. Fixed, together with multicast and IPv4-mapped IPv6 (already handled
  by Eclipse, now by the same predicate).
* **Eclipse REFUSED `198.18.0.0/15` in every form.** That is the range fake-IP proxies
  (Clash, Surge, sing-box TUN) answer every lookup from; refusing it broke every
  endpoint on such a host. The reference allows it only when the address came from a
  DNS lookup, never when it was written into the URL. Fixed the same way.
"""

from __future__ import annotations

import pytest

from harness.model_runtime.endpoint_guard import (
    EndpointBlocked,
    is_public_address,
    resolve_public_endpoint,
)


# -- the predicate, compared to the reference's rule ----------------------

#: Address the reference refuses and a naive `is_private` test lets through.
SHARED_SPACE = "100.100.100.200"

BLOCKED = [
    "127.0.0.1", "10.0.0.1", "192.168.1.1", "172.16.0.1",      # loopback / RFC1918
    "169.254.169.254",                                          # link-local metadata
    "0.0.0.0",                                                  # unspecified
    "224.0.0.1", "239.255.255.250",                             # multicast (SSDP)
    "240.0.0.1", "192.0.2.1", "203.0.113.9",                    # reserved / documentation
    "::1", "fe80::1", "fc00::1",                                # IPv6 loopback/link-local/ULA
    "::ffff:127.0.0.1", "::ffff:169.254.169.254",               # IPv4-mapped
    SHARED_SPACE, "100.64.0.1", "100.127.255.254",              # shared address space
]

ALLOWED = ["8.8.8.8", "1.1.1.1", "142.250.72.14", "2606:4700:4700::1111"]


@pytest.mark.parametrize("address", BLOCKED)
def test_non_public_addresses_are_refused(address):
    assert is_public_address(address) is False


@pytest.mark.parametrize("address", ALLOWED)
def test_public_addresses_are_allowed(address):
    assert is_public_address(address) is True


def test_the_cloud_metadata_address_is_the_finding():
    """`is_private` is False for it, which is exactly why the old check missed it."""
    import ipaddress

    assert ipaddress.ip_address(SHARED_SPACE).is_private is False
    assert is_public_address(SHARED_SPACE) is False


def test_a_fake_ip_range_answer_is_allowed_only_when_resolved():
    """Clash/Surge answer every lookup from 198.18.0.0/15; a literal in a URL is not."""
    assert is_public_address("198.18.0.5") is False
    assert is_public_address("198.18.0.5", resolved=True) is True
    assert is_public_address("198.19.255.254", resolved=True) is True
    # the carve-out is exactly that range: a private answer is still refused
    assert is_public_address("192.168.0.1", resolved=True) is False
    # and a public address just outside it needs no carve-out at all
    assert is_public_address("198.20.0.1", resolved=True) is True


def test_junk_is_not_an_address():
    assert is_public_address("not-an-ip") is False
    assert is_public_address("") is False


# -- the URL-level guard --------------------------------------------------


def test_a_metadata_literal_url_is_refused():
    with pytest.raises(EndpointBlocked) as exc:
        resolve_public_endpoint("http://100.100.100.200/v1")
    assert exc.value.code == "PRIVATE_ADDRESS"


def test_a_hostname_resolving_to_metadata_is_refused():
    with pytest.raises(EndpointBlocked) as exc:
        resolve_public_endpoint("https://evil.example/v1", resolver=["100.100.100.200"])
    assert exc.value.code == "PRIVATE_RESOLUTION"


def test_one_bad_address_among_good_ones_refuses_the_whole_host():
    """DNS rebinding returns several answers; any private one is enough to refuse."""
    with pytest.raises(EndpointBlocked):
        resolve_public_endpoint(
            "https://mixed.example/v1", resolver=["8.8.8.8", "10.0.0.5"]
        )


def test_a_fake_ip_resolution_is_allowed_for_a_real_client():
    resolved = resolve_public_endpoint(
        "https://api.example.com/v1", resolver=["198.18.0.5"]
    )
    assert resolved.addresses == ("198.18.0.5",)


def test_a_public_endpoint_passes_and_keeps_its_url():
    endpoint = resolve_public_endpoint("https://api.openai.com/v1", resolver=["8.8.8.8"])
    assert endpoint.url == "https://api.openai.com/v1"
    assert endpoint.host == "api.openai.com"


def test_allow_private_is_the_documented_local_model_escape_hatch():
    local = resolve_public_endpoint("http://127.0.0.1:11434/v1", allow_private=True)
    assert local.host == "127.0.0.1"
    endpoint = resolve_public_endpoint(
        "http://10.0.0.9", allow_private=True, resolver=["10.0.0.9"]
    )
    assert endpoint.addresses == ("10.0.0.9",)


def test_a_non_http_scheme_is_refused_before_resolution():
    for url in ("file:///etc/passwd", "ftp://example.com", "gopher://x"):
        with pytest.raises(EndpointBlocked) as exc:
            resolve_public_endpoint(url)
        assert exc.value.code == "BAD_SCHEME"


def test_an_empty_url_or_missing_host_is_named():
    with pytest.raises(EndpointBlocked) as exc:
        resolve_public_endpoint("   ")
    assert exc.value.code == "EMPTY_URL"
    with pytest.raises(EndpointBlocked) as exc:
        resolve_public_endpoint("https:///v1")
    assert exc.value.code == "NO_HOST"
