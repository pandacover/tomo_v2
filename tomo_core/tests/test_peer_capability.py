import tempfile
import unittest
from tomo_core.peer_capability import PeerCapability, PeerCapabilityError, issue_capability, load_or_create_key, verify_capability

class PeerCapabilityTests(unittest.TestCase):
    def test_signed_capability_binds_generation_and_operation(self):
        key=b'k'*32
        token=issue_capability(key,PeerCapability('owner','actor','telegram:recipient','session','generation',100,200,frozenset({'ask'})))
        claim=verify_capability(key,token,now=150,operation='ask',owner_id='owner',generation_id='generation')
        self.assertEqual(claim.destination,'telegram:recipient')
        for kwargs in ({'operation':'claim'},{'owner_id':'other'},{'generation_id':'other'}):
            with self.subTest(kwargs=kwargs),self.assertRaises(PeerCapabilityError): verify_capability(key,token,now=150,**kwargs)
    def test_rejects_excess_lifetime_future_tampering_and_unknown_operation(self):
        key=b'k'*32
        with self.assertRaises(PeerCapabilityError): issue_capability(key,PeerCapability('o','a','telegram:b','s','g',100,401,frozenset({'ask'})))
        token=issue_capability(key,PeerCapability('o','a','telegram:b','s','g',200,300,frozenset({'ask'})))
        with self.assertRaises(PeerCapabilityError): verify_capability(key,token,now=100)
        version, encoded, signature = token.split('.')
        tampered = version + '.' + ('A' if encoded[0] != 'A' else 'B') + encoded[1:] + '.' + signature
        with self.assertRaises(PeerCapabilityError): verify_capability(key,tampered,now=250)
    def test_key_uses_its_own_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(load_or_create_key(tmp),load_or_create_key(tmp))
