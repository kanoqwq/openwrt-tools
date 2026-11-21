"""Native kernel ownership migration is limited to removed clean-base kmods."""
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.ota_engine import package_replacements


class NativeKernelPackages(unittest.TestCase):
    def test_removed_base_kmod_retires_without_a_false_capability_alias(self):
        old = {'kernel':'P:kernel\nV:official-r1',
               'kmod-missing':'P:kmod-missing\nD:kernel=official-r1'}
        new = {'kernel':'P:kernel\nV:native-r1\no:unisoc-native-kernel'}
        self.assertEqual(package_replacements(old, new, {'kmod-missing'}), {'kmod-missing'})

    def test_extra_user_package_and_other_kernel_are_preserved(self):
        old = {'kernel':'P:kernel\nV:official-r1',
               'kmod-user':'P:kmod-user\nD:kernel=official-r1',
               'kmod-other':'P:kmod-other\nD:kernel=different-r1'}
        new = {'kernel':'P:kernel\nV:native-r1\no:unisoc-native-kernel'}
        self.assertEqual(package_replacements(old, new, {'kmod-other'}), set())

    def test_ordinary_provider_is_not_a_native_migration(self):
        old = {'kernel':'P:kernel\nV:official-r1',
               'kmod-old':'P:kmod-old\nD:kernel=official-r1'}
        new = {'kernel':'P:kernel\nV:other-r1\nT:unisoc-native-kernel'}
        self.assertEqual(package_replacements(old, new, {'kmod-old'}), set())

    def test_non_kmod_dependency_does_not_trigger_retirement(self):
        old = {'kernel':'P:kernel\nV:official-r1',
               'plugin':'P:plugin\nD:kernel=official-r1'}
        new = {'kernel':'P:kernel\nV:native-r1\no:unisoc-native-kernel'}
        self.assertEqual(package_replacements(old, new, {'plugin'}), set())
