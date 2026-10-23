"""签名模块的测试。AES 用 FIPS-197 标准向量验证，其余验证往返与形态。"""

import base64
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import cosy  # noqa: E402


class TestAES(unittest.TestCase):
    def test_fips197_vector(self):
        """FIPS-197 附录 C.1：AES-128 标准测试向量。"""
        key = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
        plain = bytes.fromhex("00112233445566778899aabbccddeeff")
        expected = bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a")
        got = cosy._encrypt_block(plain, cosy._expand_key(key))
        self.assertEqual(got, expected)

    def test_sbox_selfcheck(self):
        self.assertEqual(cosy._SBOX[0x00], 0x63)
        self.assertEqual(cosy._SBOX[0x53], 0xED)

    def test_cbc_padding_round_length(self):
        """CBC 输出必须是 16 的整数倍，且空明文也产生一个填充块。"""
        key = b"0123456789abcdef"
        self.assertEqual(len(cosy.aes_cbc_encrypt(b"", key)), 16)
        self.assertEqual(len(cosy.aes_cbc_encrypt(b"x" * 15, key)), 16)
        self.assertEqual(len(cosy.aes_cbc_encrypt(b"x" * 16, key)), 32)

    def test_cbc_stable_with_fixed_iv(self):
        """协议要求 key == iv 固定，所以同明文加同 key 结果必须一致。"""
        key = b"0123456789abcdef"
        self.assertEqual(
            cosy.aes_cbc_encrypt(b"hello", key),
            cosy.aes_cbc_encrypt(b"hello", key),
        )


class TestRSA(unittest.TestCase):
    def test_key_params(self):
        self.assertEqual(cosy._RSA_N.bit_length(), 1024)
        self.assertEqual(cosy._RSA_E, 65537)

    def test_ciphertext_length(self):
        self.assertEqual(len(cosy.rsa_encrypt(b"0123456789abcdef")), 128)

    def test_rejects_oversized(self):
        with self.assertRaises(ValueError):
            cosy.rsa_encrypt(b"x" * 200)

    def test_probabilistic(self):
        """PKCS#1 v1.5 用随机填充，两次密文必须不同。"""
        self.assertNotEqual(cosy.rsa_encrypt(b"abcdefgh"), cosy.rsa_encrypt(b"abcdefgh"))


class TestCustomBase64(unittest.TestCase):
    def _decode(self, enc):
        table = {ord(cosy.CUSTOM_ALPHABET[i]): ord(cosy.STD_ALPHABET[i]) for i in range(64)}
        table[ord(cosy.PAD)] = ord("=")
        std = enc.translate(table)
        n = len(std)
        a = n // 3
        return base64.b64decode((std[n - a:] + std[a:n - a] + std[:a]).encode())

    def test_roundtrip(self):
        for payload in (b"{}", b"hello world", b'{"a":1}', b"x" * 100):
            self.assertEqual(self._decode(cosy.qoder_encode(payload)), payload)

    def test_alphabet_is_permutation(self):
        self.assertEqual(len(cosy.CUSTOM_ALPHABET), 64)
        self.assertEqual(len(set(cosy.CUSTOM_ALPHABET)), 64)


class TestFingerprint(unittest.TestCase):
    def test_stable_per_uid(self):
        self.assertEqual(cosy.derive_machine_id("u1"), cosy.derive_machine_id("u1"))

    def test_distinct_across_uids(self):
        self.assertNotEqual(cosy.derive_machine_id("u1"), cosy.derive_machine_id("u2"))

    def test_uuidv4_shape(self):
        mid = cosy.derive_machine_id("u1")
        parts = mid.split("-")
        self.assertEqual([len(p) for p in parts], [8, 4, 4, 4, 12])
        self.assertEqual(parts[2][0], "4")
        self.assertEqual(parts[3][0], "a")

    def test_hostname_shape(self):
        h = cosy.derive_hostname("u1")
        self.assertTrue(h.startswith("DESKTOP-"))
        self.assertEqual(len(h), len("DESKTOP-") + 7)


class TestHeaders(unittest.TestCase):
    def _build(self, body=b"{}"):
        return cosy.build_cosy_headers(
            uid="u1", access_token="tok", nickname="n", email="e@x.com",
            machine_id="m", machine_token="mt",
            body=body,
            url="https://api3.qoder.sh/algo/api/v2/service/pro/sse/x",
            model_key="auto",
        )

    def test_required_headers(self):
        h = self._build()
        for name in ("authorization", "cosy-key", "cosy-user", "cosy-date",
                     "cosy-machineid", "cosy-bodyhash", "cosy-sigpath"):
            self.assertIn(name, h)

    def test_bearer_shape(self):
        bearer = self._build()["authorization"]
        self.assertTrue(bearer.startswith("Bearer COSY."))
        parts = bearer[len("Bearer COSY."):].split(".")
        self.assertEqual(len(parts), 2)
        self.assertEqual(len(parts[1]), 32)  # md5 hex

    def test_sigpath_strips_algo(self):
        h = self._build()
        self.assertEqual(h["cosy-sigpath"], "/api/v2/service/pro/sse/x")

    def test_bodyhash_matches(self):
        import hashlib
        body = b'{"a":1}'
        h = self._build(body)
        self.assertEqual(h["cosy-bodyhash"], hashlib.md5(body).hexdigest())
        self.assertEqual(h["cosy-bodylength"], str(len(body)))

    def test_rejects_empty_token(self):
        with self.assertRaises(ValueError):
            cosy.build_cosy_headers(
                uid="u1", access_token="", machine_id="m", machine_token="mt",
                body=b"{}", url="https://x/algo/y",
            )


if __name__ == "__main__":
    unittest.main()
