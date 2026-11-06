"""M3 is an exact tested identity on the existing U30 Air hardware adapter."""
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.model_compat import mu300_android


class M3Model(unittest.TestCase):
    def test_exact_identity_uses_existing_u30air_profile(self):
        self.assertEqual(mu300_android('MU5352', 'MU5352', 'ums9620'), ('u30air', None))

    def test_inconsistent_identity_keeps_model_warning(self):
        for model, board, platform in [('MU5352','MU5352','ums9632'),
                                       ('MU5352','other','ums9620'),
                                       ('M3','MU5352','ums9620'),
                                       ('MU5352','MU5352',''), ('M3','M3','ums9620')]:
            with self.subTest(model=model, board=board, platform=platform):
                self.assertIsNotNone(mu300_android(model, board, platform)[1])

    def test_m3_never_matches_a_native_pro_package(self):
        for expected in ('u30pro','f50pro'):
            self.assertIsNotNone(mu300_android('MU5352','MU5352','ums9620',expected)[1])

    def test_existing_products_keep_their_profiles(self):
        for model, board, platform, expected in [('U30Air','U30Air','ums9620','u30air'),
                                                 ('V50','MU3351','ums9620','v50'),
                                                 ('F50','MU300','ums9620','f50')]:
            self.assertEqual(mu300_android(model,board,platform), (expected,None))


if __name__ == '__main__': unittest.main()
