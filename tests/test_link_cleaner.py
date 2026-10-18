# Run from the repo root: venv/bin/python -m unittest discover -s tests -v
# No test touches the network.

import socket
import sys
import unittest
from pathlib import Path
from unittest import mock

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import link_cleaner  # noqa: E402
from link_cleaner import clean_url, clean_links, has_shortener_link  # noqa: E402

MAPS = (
    "https://www.google.com/maps/place/Mario+Statue,+Ørbækvej+75,+5220+Odense,+Denmark/"
    "data=!4m2!3m1!1s0x464d21006f4dba45:0xd598e4aef2cb2b6!18m1!1e1"
    "?entry=gps&coh=192189&g_ep=CAESBzI2LjQyLjEYACCenQoqxwEsMTAwODQyOTQxQgJOTA%3D%3D"
    "&skid=93ff0657-0949-4506-b8e1-e5dc156d6370&g_st=ac"
)
LINKEDIN = (
    "https://www.linkedin.com/posts/matthewjohansen_watch-your-key-usage-revoke-and-rotate-if-share-7517255937229971456-Kqfz/"
    "?highlightedUpdateUrn=urn%3Ali%3Aactivity%3A7517255938177855488"
    "&highlightedUpdateType=SOCIAL_SHARE&origin=SOCIAL_SHARE"
)


class CleanUrlTests(unittest.TestCase):
    def check(self, url, expected):
        self.assertEqual(clean_url(url), expected)

    def test_google_maps_share_params(self):
        self.check(MAPS, MAPS.split("?")[0])

    def test_linkedin_share_params(self):
        self.check(LINKEDIN, LINKEDIN.split("?")[0])

    def test_origin_only_stripped_on_linkedin(self):
        self.check("https://example.com/cb?origin=app", "https://example.com/cb?origin=app")

    def test_utm_prefix(self):
        self.check("https://example.com/a?id=5&utm_source=x&UTM_Brand_New=y&q=a,b:c",
                   "https://example.com/a?id=5&q=a,b:c")

    def test_untracked_url_left_byte_identical(self):
        url = "https://example.com/s?q=a,b:c&x=%20y&&z"
        self.check(url, url)
        self.assertEqual(clean_links(f"see {url}"), [])

    def test_modern_trackers(self):
        self.check("https://shop.example/p/1?color=red&srsltid=AfmBOo&gad_source=1"
                   "&gad_campaignid=9&_gl=1*abc&mkt_tok=NzI&mibextid=Zbw&li_fat_id=x&rdt_cid=7",
                   "https://shop.example/p/1?color=red")

    def test_microsoft_wt_mc_id_any_case(self):
        self.check("https://learn.microsoft.com/en-us/entra/?WT.mc_id=M365-MVP-1&view=x",
                   "https://learn.microsoft.com/en-us/entra/?view=x")

    def test_ocid_only_on_msn(self):
        self.check("https://www.msn.com/en-us/news/a?ocid=msedgntp&cvid=1",
                   "https://www.msn.com/en-us/news/a?cvid=1")
        self.check("https://example.com/?ocid=5", "https://example.com/?ocid=5")

    def test_google_forms_entry_fields_kept(self):
        url = "https://docs.google.com/forms/d/e/abc/viewform?entry.123=hi&usp=pp_url"
        self.check(url, url)

    def test_google_redirect_unwrap(self):
        self.check("https://www.google.com/url?q=https://site.dk/p%3Futm_medium%3De%26id%3D7&sa=D",
                   "https://site.dk/p?id=7")

    def test_amazon(self):
        self.check("https://www.amazon.com/Some-Thing/dp/B0ABCDEFGH/ref=sr_1_1?crid=X&keywords=foo&tag=aff-20",
                   "https://www.amazon.com/dp/B0ABCDEFGH?keywords=foo")
        self.check("https://www.amazon.com/de/dp/B0ABCDEFGH/ref=x", "https://www.amazon.com/de/dp/B0ABCDEFGH")
        self.check("https://www.amazon.co.uk/Thing/gp/product/B0ABCDEFGH/ref=ppx?psc=1",
                   "https://www.amazon.co.uk/gp/product/B0ABCDEFGH")
        url = "https://www.amazon.com/gp/help/customer/display.html?nodeId=123"
        self.check(url, url)

    def test_fragment_and_meaningful_params_kept(self):
        self.check("https://example.com/p?fbclid=abc#section-2", "https://example.com/p#section-2")
        self.check("https://youtu.be/dQw4w9WgXcQ?si=abc&t=42", "https://youtu.be/dQw4w9WgXcQ?t=42")

    def test_clean_links_in_chat_message(self):
        changed = clean_links(f"look at this {MAPS} and this ({LINKEDIN}).")
        self.assertEqual([c for _, c in changed], [MAPS.split("?")[0], LINKEDIN.split("?")[0]])

    def test_new_shortener_domains(self):
        for url in ("https://share.google/abc", "https://a.co/d/xyz", "https://vm.tiktok.com/Z/",
                    "https://pin.it/1", "https://spotify.link/x", "https://fb.me/x",
                    "https://maps.app.goo.gl/abc"):
            self.assertTrue(has_shortener_link(url), url)
        self.assertFalse(has_shortener_link("https://notshare.google.example/x"))


class PublicAddressTests(unittest.IsolatedAsyncioTestCase):
    def test_is_public_ip(self):
        for addr in ("169.254.169.254", "168.63.129.16", "127.0.0.1", "10.0.0.5",
                     "172.16.0.4", "192.168.1.1", "100.64.0.1", "0.0.0.0", "224.0.0.1",
                     "::1", "fe80::1", "::ffff:127.0.0.1", "::ffff:169.254.169.254", "not-an-ip"):
            self.assertFalse(link_cleaner._is_public_ip(addr), addr)
        for addr in ("1.1.1.1", "8.8.8.8", "2606:4700:4700::1111"):
            self.assertTrue(link_cleaner._is_public_ip(addr), addr)

    async def test_ip_literal_hosts_rejected_up_front(self):
        # aiohttp connects to IP literals without asking the resolver, so the
        # up-front check is what stops http://169.254.169.254/ style redirects.
        for host in ("169.254.169.254", "168.63.129.16", "127.0.0.1"):
            self.assertFalse(await link_cleaner._is_public_host(host), host)
        self.assertTrue(await link_cleaner._is_public_host("1.1.1.1"))


class _FakeInnerResolver:
    def __init__(self, ip):
        self.ip = ip

    async def resolve(self, host, port=0, family=socket.AF_INET):
        return [{"hostname": host, "host": self.ip, "port": port, "family": family,
                 "proto": 0, "flags": 0}]

    async def close(self):
        pass


class DnsRebindingTests(unittest.IsolatedAsyncioTestCase):
    def resolver(self, ip):
        r = link_cleaner._PublicOnlyResolver()
        r._inner = _FakeInnerResolver(ip)
        return r

    async def test_resolver_refuses_internal_and_allows_public(self):
        with self.assertRaises(link_cleaner._BlockedAddress):
            await self.resolver("169.254.169.254").resolve("evil.test")
        infos = await self.resolver("93.184.216.34").resolve("ok.test")
        self.assertEqual(infos[0]["host"], "93.184.216.34")

    async def test_rebinding_after_upfront_check_is_blocked_and_reported(self):
        # Up-front lookup says "public", then DNS answers 127.0.0.1 at connect
        # time. The connection must be refused and surfaced as UnsafeRedirect.
        connector = aiohttp.TCPConnector(resolver=self.resolver("127.0.0.1"))
        async with aiohttp.ClientSession(connector=connector) as session:
            with mock.patch.object(link_cleaner, "_is_public_host", mock.AsyncMock(return_value=True)):
                with self.assertRaises(link_cleaner.UnsafeRedirect):
                    await link_cleaner._resolve_shortener(session, "http://rebind.test/x")


if __name__ == "__main__":
    unittest.main()
