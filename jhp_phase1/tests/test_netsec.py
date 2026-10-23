"""NetSecKit pure-Python port — golden vectors + round-trip invariants.

No network. The live-server proof (200 + RSA signature match) is recorded in
archive/unidbg-era/SESSION_MUMU_TOKEN_20261012.md; these tests keep the offline
contract from regressing.
"""
import json
import os
import sys
import unittest

# Application root, computed so the tree can move.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from replica.transport import netsec


class TestFold32(unittest.TestCase):
    def test_formula(self):
        # h = (((h & 0xffffff) << 8) | c) ^ (h >> 24)
        self.assertEqual(netsec.fold32(''), 0)
        self.assertEqual(netsec.fold32('a'), ord('a'))
        self.assertEqual(netsec.fold32('ab'), (ord('a') << 8) | ord('b'))

    def test_upstream_vectors(self):
        got = [netsec.fold32(v) for v in
               ('nhang.school', '5K0E8400-E29', '597DEA1AFB49', '86123.456789')]
        self.assertEqual(got, [0x61297D61, 0x203A324C, 0x363A323C, 0x3D2F3D3E])

    def test_nul_termination(self):
        self.assertEqual(netsec.fold32('abc\x00def'), netsec.fold32('abc'))


class TestDeriveKey(unittest.TestCase):
    def test_upstream_vector(self):
        key = netsec.derive_paes_key('nhang.school', '5K0E8400-E29',
                                     '597DEA1AFB49', '86123.456789')
        self.assertEqual(key.hex(), 'faaed5a4d99af386a7d3f5d109071f13')

    def test_length_is_16(self):
        k = netsec.derive_paes_key('nhang.school', '5K0E8400-E29',
                                   '597DEA1AFB49', '86123.456789')
        self.assertEqual(len(k), 16)

    def test_rejects_bad_lengths(self):
        with self.assertRaises(ValueError):
            netsec.derive_paes_key('short', '5K0E8400-E29', '597DEA1AFB49', '86123.456789')
        with self.assertRaises(ValueError):
            netsec.derive_paes_key('nhang.school', '5K0E8400-E29', '597DEA1AFB49',
                                   'x' * 13)


class TestEnvelope(unittest.TestCase):
    def setUp(self):
        self.sess = netsec.EnvelopeSession(four='86123.456789')

    def test_container_key_order(self):
        env = netsec.build_envelope(self.sess, '{"a":1}', 'insert')
        obj = json.loads(env['container'])
        self.assertEqual(list(obj.keys()),
                         ['data', 'timeStamp', 'platform',
                          'keyDataOne', 'keyDataTwo', 'keyDataThree', 'keyDataFour'])
        self.assertEqual(obj['keyDataOne'], 'nhang.school')
        self.assertEqual(obj['keyDataTwo'], '5K0E8400-E29')
        self.assertEqual(obj['keyDataThree'], '597DEA1AFB49')
        self.assertEqual(obj['keyDataFour'], '86123.456789')

    def test_container_roundtrip(self):
        env = netsec.build_envelope(self.sess, '{"hello":"世界"}', 'insert')
        back = netsec.aes_cbc_decrypt(env['req_key'].encode(),
                                      netsec.b64d(env['d']))
        self.assertEqual(back, env['container'])

    def test_h_is_md5_of_container(self):
        env = netsec.build_envelope(self.sess, '{}', 'insert')
        self.assertEqual(env['h'], netsec.md5_hex(env['container']))

    def test_req_key_shape(self):
        env = netsec.build_envelope(self.sess, '{}', 'insert')
        self.assertEqual(len(env['req_key']), 16)
        self.assertTrue(all(c in netsec.SALT_ALPHABET for c in env['req_key']))

    def test_rsa_blob_is_1024_bits(self):
        env = netsec.build_envelope(self.sess, '{}', 'insert')
        self.assertEqual(len(netsec.b64d(env['k'])), 128)

    def test_outer_key_orders(self):
        ins = json.loads(netsec.build_envelope(self.sess, '{}', 'insert')['json'])
        obs = json.loads(netsec.build_envelope(self.sess, '{}', 'observed')['json'])
        self.assertEqual(list(ins.keys()), ['d', 'h', 'k', 'p', 't'])
        self.assertEqual(list(obs.keys()), ['k', 'p', 'd', 'h', 't'])
        self.assertEqual(ins['p'], 101)
        self.assertEqual(obs['p'], 101)
        self.assertEqual(ins['t'], 0)
        self.assertEqual(obs['t'], 0)
        # headerSign and body are independent encryptions (fresh reqKey each),
        # so their d/k MUST differ — only the constants are shared.
        self.assertNotEqual(ins['d'], obs['d'])
        self.assertNotEqual(ins['k'], obs['k'])

    def test_per_request_req_key_is_fresh(self):
        a = netsec.build_envelope(self.sess, '{}', 'insert')
        b = netsec.build_envelope(self.sess, '{}', 'insert')
        self.assertNotEqual(a['req_key'], b['req_key'])
        self.assertNotEqual(a['d'], b['d'])

    def test_four_is_latched(self):
        sess = netsec.EnvelopeSession()
        first = sess.key_data()[3]
        self.assertEqual(sess.key_data()[3], first)
        self.assertEqual(len(first), 12)

    def test_four_from_ms_formats(self):
        self.assertEqual(netsec.key_data_four_from_ms(1791815536447),
                         netsec.key_data_four_from_ms(1791815536447))
        self.assertEqual(len(netsec.key_data_four_from_ms(1791815536447)), 12)


class TestDecryptResponse(unittest.TestCase):
    """The response signature cannot be produced without the server's private
    key, so the verify step is mocked: everything else is real."""

    def setUp(self):
        from unittest import mock
        self.sess = netsec.EnvelopeSession(four='86123.456789')
        self._mock = mock
        self._patch = None

    def _encrypt_layer(self, text):
        return netsec.b64e(netsec.aes_cbc_encrypt(self.sess.paes_key(),
                                                  text.encode()))

    def _expect_md5(self, md5_value):
        self._patch = self._mock.patch.object(
            netsec, 'rsa_recover_digest', lambda s: md5_value)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def test_plaintext_passthrough(self):
        raw = json.dumps({'error': 401, 'message': 'Unauthorized'})
        dec = netsec.decrypt_response(raw, self.sess)
        self.assertFalse(dec['encrypted'])
        self.assertEqual(dec['business']['error'], 401)

    def test_two_layer_roundtrip(self):
        inner = json.dumps({'data': netsec.b64e(
            json.dumps({'error': 10000, 'data': {'x': 1}}).encode())})
        layer1 = json.dumps({'d': self._encrypt_layer(inner)})
        self._expect_md5(netsec.md5_hex(layer1.encode()))
        resp = json.dumps({'r': self._encrypt_layer(layer1),
                           's': netsec.b64e(b'\x00' * 128), 'v': 101})
        dec = netsec.decrypt_response(resp, self.sess)
        self.assertTrue(dec['encrypted'])
        self.assertEqual(dec['business'], {'error': 10000, 'data': {'x': 1}})
        self.assertEqual(dec['actual_md5'], netsec.md5_hex(layer1.encode()))

    def test_single_layer_roundtrip(self):
        # `r` decrypts straight to {"data": b64(biz)} without an inner `d`.
        layer1 = json.dumps({'data': netsec.b64e(
            json.dumps({'error': 10000, 'data': [1, 2]}).encode())})
        self._expect_md5(netsec.md5_hex(layer1.encode()))
        resp = json.dumps({'r': self._encrypt_layer(layer1),
                           's': netsec.b64e(b'\x00' * 128), 'v': 101})
        dec = netsec.decrypt_response(resp, self.sess)
        self.assertEqual(dec['business'], {'error': 10000, 'data': [1, 2]})

    def test_bad_signature_is_fail_closed(self):
        layer1 = json.dumps({'d': self._encrypt_layer('{"data":"e30="}')})
        self._expect_md5('deadbeef' * 4)
        resp = json.dumps({'r': self._encrypt_layer(layer1),
                           's': netsec.b64e(b'\x00' * 128), 'v': 101})
        with self.assertRaises(netsec.DecryptError):
            netsec.decrypt_response(resp, self.sess)

    def test_bad_version(self):
        resp = json.dumps({'r': 'AAAA', 's': 'AAAA', 'v': 100})
        with self.assertRaises(netsec.DecryptError):
            netsec.decrypt_response(resp, self.sess)

    def test_non_json_rejected(self):
        with self.assertRaises(netsec.DecryptError):
            netsec.decrypt_response('<html>502</html>', self.sess)

    def test_resp_wrapper_unwrapped(self):
        raw = json.dumps({'resp': json.dumps({'error': 401, 'message': 'x'})})
        dec = netsec.decrypt_response(raw, self.sess)
        self.assertEqual(dec['business']['error'], 401)


class TestRsaRecovery(unittest.TestCase):
    def test_rejects_wrong_length(self):
        with self.assertRaises(ValueError):
            netsec.rsa_recover_digest(netsec.b64e(b'\x00' * 64))

    def test_rejects_non_type1(self):
        with self.assertRaises(ValueError):
            netsec.rsa_recover_digest(netsec.b64e(b'\x00' * 128))


if __name__ == '__main__':
    unittest.main(verbosity=2)
