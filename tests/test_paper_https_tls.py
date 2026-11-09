"""HTTPS probe contract: injected HTTP responses only, no external requests."""
import ssl
import unittest
from unittest.mock import patch
import paper_https_setup as p


class Reply:
    def __init__(self, body, url='https://example.com/'):
        self.body=body;self.url=url;self.bounds=[]
        self.headers={'X-AIImpuls-Paper-Instance':'a'*32}
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def read(self,bound):self.bounds.append(bound);return self.body


class PaperTLSProbeTest(unittest.TestCase):
    def probe(self, reply):
        seen={}
        class Opener:
            def open(self, request, timeout):
                seen['request']=request;seen['timeout']=timeout
                return reply
        def build(*handlers):seen['handlers']=handlers;return Opener()
        with patch.object(p.urllib.request,'build_opener',side_effect=build):
            p.Windows().verify_certificate('https://example.com','a'*32)
        return seen

    def test_native_trust_bounded_paper_response_no_credentials(self):
        reply=Reply(b'<body data-controller="native" data-role="paper">')
        seen=self.probe(reply)
        handler=next(h for h in seen['handlers'] if isinstance(h,p.urllib.request.HTTPSHandler))
        self.assertEqual(handler._context.verify_mode,ssl.CERT_REQUIRED)
        self.assertTrue(handler._context.check_hostname)
        self.assertLessEqual(max(reply.bounds),2*1024*1024+1)
        self.assertEqual(seen['request'].full_url,'https://example.com')
        self.assertFalse(seen['request'].has_header('Authorization'))
        self.assertFalse(seen['request'].has_header('Cookie'))
        redirect=next(h for h in seen['handlers'] if isinstance(h,p.urllib.request.HTTPRedirectHandler))
        with self.assertRaises(p.shared.HttpsError):
            redirect.redirect_request(None,None,302,'redirect',{},'https://another.example')

    def test_live_or_generic_page_not_paper(self):
        for body in (b'<body data-controller="native" data-role="live">',b'panel.js',b'login'):
            with self.subTest(body=body),self.assertRaises(p.shared.HttpsError):self.probe(Reply(body))

    def test_unexpected_final_address_rejected(self):
        with self.assertRaises(p.shared.HttpsError):
            self.probe(Reply(b'<body data-controller="native" data-role="paper">','https://other.example/'))

    def test_certificate_failure_never_bypassed(self):
        class Opener:
            def open(self,*args,**kwargs):raise ssl.SSLCertVerificationError('untrusted')
        with patch.object(p.urllib.request,'build_opener',return_value=Opener()),self.assertRaises(ssl.SSLCertVerificationError):
            p.Windows().verify_certificate('https://example.com','a'*32)

    def test_wrong_proxy_identity_rejected(self):
        reply=Reply(b'<body data-controller="native" data-role="paper">')
        reply.headers['X-AIImpuls-Paper-Instance']='b'*32
        with self.assertRaises(p.shared.HttpsError):self.probe(reply)

    def test_oversized_page_rejected(self):
        reply=Reply(b'<body data-controller="native" data-role="paper">'+b'x'*(2*1024*1024))
        with self.assertRaises(p.shared.HttpsError):self.probe(reply)
