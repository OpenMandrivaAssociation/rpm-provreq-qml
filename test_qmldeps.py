#!/usr/bin/python
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the QML Provides/Requires generator."""

import ctypes
import importlib.machinery
import importlib.util
import os
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "qmldeps")

loader = importlib.machinery.SourceFileLoader("qmldeps", SCRIPT)
spec = importlib.util.spec_from_loader("qmldeps", loader)
qd = importlib.util.module_from_spec(spec)
loader.exec_module(qd)


def be16(n):
    return n.to_bytes(2, "big")


def be32(n):
    return n.to_bytes(4, "big")


def be64(n):
    return n.to_bytes(8, "big")


def name_record(text):
    return be16(len(text)) + be32(qd.qt_hash(text)) + text.encode("utf-16-be")


def file_node(name_off, data_off, flags=0):
    return be32(name_off) + be16(flags) + be16(0) + be16(0) + be32(data_off) + be64(0)


def dir_node(name_off, count, first):
    return be32(name_off) + be16(qd.DIRECTORY) + be32(count) + be32(first) + be64(0)


def data_record(payload):
    return be32(len(payload)) + payload


def qcompress(payload, level=9):
    return be32(len(payload)) + zlib.compress(payload, level)


def zstd_compress(payload):
    lib = ctypes.CDLL("libzstd.so.1")
    lib.ZSTD_compress.argtypes = [
        ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int
    ]
    lib.ZSTD_compress.restype = ctypes.c_size_t
    lib.ZSTD_compressBound.argtypes = [ctypes.c_size_t]
    lib.ZSTD_compressBound.restype = ctypes.c_size_t
    lib.ZSTD_isError.argtypes = [ctypes.c_size_t]
    lib.ZSTD_isError.restype = ctypes.c_uint
    bound = lib.ZSTD_compressBound(len(payload))
    dst = ctypes.create_string_buffer(bound)
    src = ctypes.create_string_buffer(payload)
    n = lib.ZSTD_compress(dst, bound, src, len(payload), 3)
    if lib.ZSTD_isError(n):
        raise RuntimeError("zstd compress failed")
    return dst.raw[:n]


QMLDIR = """\
module Example.Mod
depends QtQuick auto
import Extra.Mod 1.0
optional import Optional.Mod 1.0
default import Default.Mod auto
static
system
Widget 1.0 Widget.qml
Widget 2.1 Widget.qml
Module 1.2 Module.qml
singleton Thing 0.0 Thing.qml
internal Secret Secret.qml
# depends Commented.Out
"""

MAIN_QML = """\
import QtQuick
import Example.Mod 1.0 as Example
import org.kde.kcmutils as KCM
import org.kde.kinfocenter.private 1.0 as KInfoCenter
import "widgets"
// import Hidden.Mod
/* import Also.Hidden
   import Still.Hidden */
import Visible.Mod
Item {
}
"""

CPU_QML = """\
/*
    SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
    SPDX-FileCopyrightText: 2021 Harald Sitter <sitter@kde.org>
*/

import QtQuick 2.5

import org.kde.kcmutils as KCM

import org.kde.kinfocenter.private 1.0 as KInfoCenter

KInfoCenter.CommandOutputKCM {
    output: kcm.infoOutputContext
    wrapMode: TextEdit.Wrap
}
"""


def build_tree(payloads, flags_for):
    """payloads: list of (filename, raw_bytes). flags_for(name, raw) -> (flags, stored)."""
    names = b""
    offsets = []
    for filename, _raw in payloads:
        offsets.append(len(names))
        names += name_record(filename)
    stored = []
    data = b""
    data_offs = []
    flags = []
    for filename, raw in payloads:
        flag, blob = flags_for(filename, raw)
        data_offs.append(len(data))
        data += data_record(blob)
        stored.append(blob)
        flags.append(flag)
    nodes = [dir_node(0, len(payloads), 1)]
    for off, data_off, flag in zip(offsets, data_offs, flags):
        nodes.append(file_node(off, data_off, flag))
    return b"".join(nodes), names, data


def wrap_rcc(tree, names, data, version=3):
    header_len = 24 if version >= 3 else 20
    tree_off = header_len
    names_off = tree_off + len(tree)
    data_off = names_off + len(names)
    header = bytearray(header_len)
    header[0:4] = b"qres"
    header[4:8] = be32(version)
    header[8:12] = be32(tree_off)
    header[12:16] = be32(data_off)
    header[16:20] = be32(names_off)
    return bytes(header) + tree + names + data


def wrap_elf(tree, names, data):
    # Data sits well before the tree so a single-file scan has to use the
    # compression magic, not the bytes next to the root node.
    gap = b"\x00" * 8000
    return (
        b"\x7fELF"
        + b"\x00" * 12
        + qd.QT_REGISTER
        + b"\x00" * 32
        + data
        + gap
        + names
        + gap
        + tree
    )


class ParserTests(unittest.TestCase):
    def test_version_compare_matches_shell(self):
        self.assertFalse(qd.version_newer("1.0", "2.0"))
        self.assertTrue(qd.version_newer("2.0", "1.0"))
        self.assertFalse(qd.version_newer("1.0", "1.0"))
        self.assertTrue(qd.version_newer("1.0", ""))
        self.assertTrue(qd.version_newer("1.2.3.4", "1.2"))
        self.assertTrue(qd.version_newer("1.10", "1.9"))
        self.assertFalse(qd.version_newer("0.0", ""))
        self.assertFalse(qd.version_newer("0", ""))
        self.assertFalse(qd.version_newer("1.0", "1"))
        self.assertFalse(qd.version_newer("static", "1.0"))

    def test_qml_import_drops_version_and_qualifier(self):
        modules = qd.qml_imports(CPU_QML)
        self.assertEqual(
            modules,
            {"QtQuick", "org.kde.kcmutils", "org.kde.kinfocenter.private"},
        )
        modules = qd.qml_imports(MAIN_QML)
        self.assertEqual(
            modules,
            {
                "QtQuick",
                "Example.Mod",
                "org.kde.kcmutils",
                "org.kde.kinfocenter.private",
                "Visible.Mod",
            },
        )

    def test_qmldir_provides_and_requires(self):
        provides, requires = qd.parse_qmldir(QMLDIR)
        self.assertEqual(
            set(provides),
            {
                "qml(Example.Mod.Widget) = 1.0",
                "qml(Example.Mod.Widget) = 2.1",
                "qml(Example.Mod.Module) = 1.2",
                "qml(Example.Mod.Thing) = 0.0",
                "qml(Example.Mod) = 2.1",
            },
        )
        self.assertEqual(requires, {"QtQuick", "Extra.Mod"})

    def test_tooling_module_type_is_not_a_keyword(self):
        tooling = "/usr/lib64/qt6/qml/QtQuick/tooling/qmldir"
        if os.path.isfile(tooling):
            with open(tooling, encoding="utf-8") as fh:
                text = fh.read()
        else:
            text = (
                "module QtQuick.tooling\n"
                "linktarget Qt6::quicktooling\n"
                "Module 1.2 Module.qml\n"
                "Module 6.0 Module.qml\n"
            )
        provides, _requires = qd.parse_qmldir(text)
        self.assertIn("qml(QtQuick.tooling.Module) = 1.2", provides)
        self.assertIn("qml(QtQuick.tooling.Module) = 6.0", provides)
        self.assertIn("qml(QtQuick.tooling) = 6.0", provides)

    def test_bare_static_and_system_are_not_types(self):
        provides, requires = qd.parse_qmldir(
            "module QML\ndesignersupported\nstatic\nsystem\n"
        )
        self.assertEqual(provides, ["qml(QML)"])
        self.assertEqual(requires, set())

    def test_zero_version_leaves_module_unversioned(self):
        provides, requires = qd.parse_qmldir(
            "module org.kde.kwin.decoration\n"
            "optional plugin decorationplugin\n"
            "Decoration 0.0 Decoration.qml\n"
            "depends QtQuick\n"
        )
        self.assertEqual(
            set(provides),
            {
                "qml(org.kde.kwin.decoration.Decoration) = 0.0",
                "qml(org.kde.kwin.decoration)",
            },
        )
        self.assertEqual(requires, {"QtQuick"})

    def test_no_module_line_has_no_provides(self):
        provides, requires = qd.parse_qmldir(
            "singleton MobileTextActionsToolBar 1.0 MobileTextActionsToolBar.qml\n"
            "MobileCursor 1.0 MobileCursor.qml\n"
        )
        self.assertEqual(provides, [])
        self.assertEqual(requires, set())

    def test_controls_optional_and_default_are_not_requires(self):
        text = (
            "module QtQuick.Controls\n"
            "optional import QtQuick.Controls.Fusion auto\n"
            "optional import QtQuick.Controls.Material auto\n"
            "default import QtQuick.Controls.Basic auto\n"
            "depends QtQuick auto\n"
        )
        provides, requires = qd.parse_qmldir(text)
        self.assertEqual(provides, ["qml(QtQuick.Controls)"])
        self.assertEqual(requires, {"QtQuick"})


class ResourceTests(unittest.TestCase):
    def expected_from(self, texts, mode):
        return qd.deps_from_texts(texts, mode)

    def assert_roundtrip(self, blob, path):
        texts = qd.extract_texts(path, blob)
        got = {name: text for name, text in texts}
        self.assertEqual(got.get("qmldir"), QMLDIR)
        self.assertEqual(got.get("Main.qml"), MAIN_QML)
        requires = set(qd.deps_from_texts(texts, "requires"))
        self.assertEqual(
            requires,
            {
                "qml(QtQuick)",
                "qml(Extra.Mod)",
                "qml(Example.Mod)",
                "qml(org.kde.kcmutils)",
                "qml(org.kde.kinfocenter.private)",
                "qml(Visible.Mod)",
            },
        )
        provides = set(qd.deps_from_texts(texts, "provides"))
        self.assertIn("qml(Example.Mod) = 2.1", provides)
        self.assertIn("qml(Example.Mod.Module) = 1.2", provides)
        self.assertNotIn("qml(Optional.Mod)", requires)
        self.assertNotIn("qml(Default.Mod)", requires)

    def test_uncompressed_rcc_and_elf(self):
        raw = [( "qmldir", QMLDIR.encode()), ("Main.qml", MAIN_QML.encode())]
        tree, names, data = build_tree(raw, lambda _n, blob: (0, blob))
        self.assert_roundtrip(wrap_rcc(tree, names, data), "mod.rcc")
        elf = b"\x7fELF" + qd.QT_REGISTER + b"\x00" * 64 + data + names + tree
        self.assert_roundtrip(elf, "plugin.so")

    def test_zlib_and_zstd_far_from_the_tree(self):
        raw = [("qmldir", QMLDIR.encode()), ("Main.qml", MAIN_QML.encode())]

        def zlib_flags(_name, blob):
            return qd.COMPRESSED_ZLIB, qcompress(blob)

        tree, names, data = build_tree(raw, zlib_flags)
        self.assert_roundtrip(wrap_elf(tree, names, data), "zlib.so")

        def zstd_flags(_name, blob):
            return qd.COMPRESSED_ZSTD, zstd_compress(blob)

        tree, names, data = build_tree(raw, zstd_flags)
        self.assert_roundtrip(wrap_rcc(tree, names, data), "zstd.rcc")
        # Single compressed document, parked away from the tree.
        one = [("Main.qml", MAIN_QML.encode())]
        tree, names, data = build_tree(one, zstd_flags)
        elf = wrap_elf(tree, names, data)
        texts = qd.extract_texts("one.so", elf)
        self.assertEqual(texts, [("Main.qml", MAIN_QML)])

    def test_utf16_gate_skips_resources_without_qml_names(self):
        called = []

        def boom(_buf):
            called.append(True)
            raise AssertionError("should not scan")

        orig = qd.extract_embedded
        qd.extract_embedded = boom
        try:
            data = b"\x7fELF" + qd.QT_REGISTER + b"\x00" * 64 + b".qml"
            self.assertEqual(qd.extract_texts("x.so", data), [])
            self.assertEqual(called, [])
        finally:
            qd.extract_embedded = orig

    def test_rcc_binary_if_available(self):
        rcc = "/usr/lib64/qt6/libexec/rcc"
        if not os.access(rcc, os.X_OK):
            self.skipTest("rcc not installed")
        with tempfile.TemporaryDirectory() as tmp:
            qmldir = os.path.join(tmp, "qmldir")
            qml = os.path.join(tmp, "Main.qml")
            qrc = os.path.join(tmp, "mod.qrc")
            out = os.path.join(tmp, "mod.rcc")
            with open(qmldir, "w", encoding="utf-8") as fh:
                fh.write(QMLDIR)
            with open(qml, "w", encoding="utf-8") as fh:
                fh.write(MAIN_QML)
            with open(qrc, "w", encoding="utf-8") as fh:
                fh.write(
                    "<RCC><qresource prefix=\"/qt/qml/Example/Mod\">"
                    f"<file alias=\"qmldir\">{qmldir}</file>"
                    f"<file alias=\"Main.qml\">{qml}</file>"
                    "</qresource></RCC>\n"
                )
            for args in (
                ["--binary", "--no-compress"],
                ["--binary", "--compress-algo", "zlib", "--threshold", "0"],
                ["--binary", "--compress-algo", "zstd", "--threshold", "0"],
            ):
                subprocess.run([rcc, *args, "-o", out, qrc], check=True)
                with open(out, "rb") as fh:
                    blob = fh.read()
                self.assertTrue(blob.startswith(b"qres"), args)
                self.assert_roundtrip(blob, out)


class CommandTests(unittest.TestCase):
    def test_multifile_and_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            qml = os.path.join(tmp, "Main.qml")
            empty = os.path.join(tmp, "note.txt")
            with open(qml, "w", encoding="utf-8") as fh:
                fh.write(CPU_QML)
            with open(empty, "w", encoding="utf-8") as fh:
                fh.write("hello\n")
            proc = subprocess.run(
                [sys.executable, SCRIPT, "--requires", "--multifile"],
                input=qml + "\n" + empty + "\n",
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(
                proc.stdout.splitlines(),
                [
                    ";" + qml,
                    "qml(QtQuick)",
                    "qml(org.kde.kcmutils)",
                    "qml(org.kde.kinfocenter.private)",
                ],
            )
            bad = subprocess.run(
                [sys.executable, SCRIPT, "--no-such"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(bad.returncode, 2)
            # A directory is not QML. OSError stays inside the scanner so
            # rpmbuild does not fail and no dependencies are emitted.
            broken = subprocess.run(
                [sys.executable, SCRIPT, "--provides", tmp],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(broken.returncode, 0)
            self.assertEqual(broken.stdout, "")
            env = os.environ.copy()
            env["RPM_QML_REQUIRES"] = "0"
            muted = subprocess.run(
                [sys.executable, SCRIPT, "--requires", qml],
                capture_output=True,
                text=True,
                check=False,
                env=env,
            )
            self.assertEqual(muted.returncode, 0)
            self.assertEqual(muted.stdout, "")

    def test_libc_has_no_qml(self):
        libc = "/usr/lib64/libc.so.6"
        if not os.path.exists(libc):
            self.skipTest("libc not at the usual path")
        self.assertEqual(qd.deps_for_path(libc, "requires"), [])
        self.assertEqual(qd.deps_for_path(libc, "provides"), [])


class InstalledBinaryTests(unittest.TestCase):
    def test_kinfocenter_cpu_plugin(self):
        path = "/usr/lib64/qt6/plugins/plasma/kcms/kinfocenter/kcm_cpu.so"
        if not os.path.exists(path):
            self.skipTest("kinfocenter cpu plugin not installed")
        with open(path, "rb") as fh:
            texts = dict(qd.extract_texts(path, fh.read()))
        self.assertIn("main.qml", texts)
        requires = qd.deps_from_texts(list(texts.items()), "requires")
        self.assertEqual(
            set(requires),
            {"qml(" + name + ")" for name in qd.qml_imports(texts["main.qml"])},
        )
        for dep in requires:
            self.assertNotIn(">=", dep)
            self.assertNotIn(" as", dep)
        if "import org.kde.kcmutils as KCM" in texts["main.qml"]:
            self.assertIn("qml(org.kde.kcmutils)", requires)

    def test_libkwin_provides_org_kde_kwin(self):
        path = "/usr/lib64/libkwin.so.6"
        if not os.path.exists(path):
            self.skipTest("libkwin not installed")
        provides = qd.deps_for_path(path, "provides")
        self.assertIn("qml(org.kde.kwin)", provides)

    def test_systemsettings_requires_are_unversioned(self):
        path = "/usr/bin/systemsettings"
        if not os.path.exists(path):
            self.skipTest("systemsettings not installed")
        with open(path, "rb") as fh:
            texts = qd.extract_texts(path, fh.read())
        names = [name for name, _text in texts]
        self.assertIn("qmldir", names)
        self.assertGreaterEqual(sum(name.endswith(".qml") for name in names), 1)
        requires = qd.deps_for_path(path, "requires")
        self.assertTrue(requires)
        for dep in requires:
            self.assertTrue(dep.startswith("qml("))
            self.assertNotIn(">=", dep)


# ELF64 LE. Virtual addresses are independent of file offsets.
SHT_PROGBITS = 1
SHT_STRTAB = 3
SHT_RELA = 4
SHT_DYNSYM = 11
EM_X86_64 = 62
EM_AARCH64 = 183


def _strtab(strings):
    blob = bytearray(b"\0")
    offsets = {}
    for text in strings:
        if text in offsets:
            continue
        offsets[text] = len(blob)
        blob += text.encode() + b"\0"
    return bytes(blob), offsets


def build_elf(machine, parts, symbols, relas=None):
    """Tiny ELF64 shared object.

    parts: (name, type, flags, addr, entsize, data)
    symbols: (name, value, shndx) after the null symbol. shndx 0 is undefined.
    relas: (got_addr, symbol_index) with symbol_index 1-based.
    """
    dynstr, dynoff = _strtab([name for name, _value, _shndx in symbols])
    dynsym = [struct.pack("<IBBHQQ", 0, 0, 0, 0, 0, 0)]
    for name, value, shndx in symbols:
        info = 0x12 if shndx else 0x10
        dynsym.append(struct.pack("<IBBHQQ", dynoff[name], info, 0, shndx, value, 0))
    rela = b""
    for got, symi in relas or ():
        rela += struct.pack("<QQq", got, (symi << 32) | 7, 0)
    items = [("", 0, 0, 0, 0, b"")]
    items.extend(parts)
    items.append((".dynstr", SHT_STRTAB, 0, 0, 0, dynstr))
    items.append((".dynsym", SHT_DYNSYM, 0, 0, 24, b"".join(dynsym)))
    if relas:
        items.append((".rela.plt", SHT_RELA, 0, 0, 24, rela))
    shstr, nameoff = _strtab([name for name, *_rest in items] + [".shstrtab"])
    items.append((".shstrtab", SHT_STRTAB, 0, 0, 0, shstr))
    blob = bytearray(64)
    offsets = []
    for _name, _typ, _flags, _addr, _entsize, data in items:
        while len(blob) % 8:
            blob.append(0)
        offsets.append(len(blob))
        blob += data
    while len(blob) % 8:
        blob.append(0)
    shoff = len(blob)
    for (name, typ, flags, addr, entsize, data), off in zip(items, offsets):
        blob += struct.pack(
            "<IIQQQQIIQQ", nameoff[name], typ, flags, addr, off, len(data), 0, 0, 1, entsize
        )
    struct.pack_into(
        "<HHIQQQIHHHHHH", blob, 16,
        3, machine, 1, 0, 0, shoff, 0, 64, 0, 0, 64, len(items), len(items) - 1,
    )
    blob[0:4] = b"\x7fELF"
    blob[4] = 2
    blob[5] = 1
    blob[6] = 1
    return bytes(blob)


def x86_plt(index, stub_va, got):
    disp = got - (stub_va + 6)
    return (
        b"\xff\x25" + struct.pack("<i", disp)
        + b"\x68" + struct.pack("<I", index)
        + b"\xe9\x00\x00\x00\x00"
    )


def x86_call(at, target):
    return b"\xe8" + struct.pack("<i", target - (at + 5))


def x86_lea_rip(at, reg, target):
    rex = 0x48 if reg < 8 else 0x4C
    reg3 = reg if reg < 8 else reg - 8
    return bytes([rex, 0x8D, (reg3 << 3) | 5]) + struct.pack("<i", target - (at + 7))


def itanium_register_types(suffix):
    body = "qml_register_types_" + suffix
    return f"_Z{len(body)}{body}v"


def elf_with(machine, text, rodata=b"", symbols=(), plt=b"", text_va=0x1000, rodata_va=0x2000, plt_va=0x3000):
    parts = []
    if text:
        parts.append((".text", SHT_PROGBITS, 6, text_va, 0, text))
    if rodata:
        parts.append((".rodata", SHT_PROGBITS, 2, rodata_va, 0, rodata))
    if plt:
        parts.append((".plt", SHT_PROGBITS, 6, plt_va, 0, plt))
    relas = [(0x4000, 1)] if plt and symbols else None
    # One GOT slot is enough when every stub in `plt` was built for got 0x4000.
    # Callers that need several stubs pass relas through symbols' order and a
    # prebuilt plt; the single-stub helper below is the common case.
    if plt and symbols and len(symbols) >= 1:
        # Replace with one rela per 16-byte stub, GOT 0x4000 + 8*n, only when
        # the caller used x86_plt's matching got. Tests that need a custom GOT
        # pass plt already matched to 0x4000.
        relas = []
        for index in range(len(symbols)):
            relas.append((0x4000 + 8 * index, index + 1))
    return build_elf(machine, parts, list(symbols), relas)


class CppProvideTests(unittest.TestCase):
    """C++ registration provides. Encodings are the ones clang and gcc emit."""

    def test_symbol_suffix_does_not_swallow_the_void_parameter(self):
        name = itanium_register_types("org_kde_newstuff")
        self.assertEqual(qd.register_types_suffix(name), "org_kde_newstuff")
        self.assertEqual(qd.register_types_suffix(name + "@Qt_6"), "org_kde_newstuff")
        self.assertIsNone(qd.register_types_suffix("qml_register_types_org_kde_newstuff"))
        self.assertEqual(
            qd.module_symbol("org.kde.kwin_x11"),
            qd.module_symbol("org.kde.kwin.x11"),
        )

    def test_defined_symbol_needs_one_matching_string(self):
        rodata = b"org.kde.newstuff\0QtQuick\0QML.Element\0"
        sym = itanium_register_types("org_kde_newstuff")
        elf = elf_with(EM_X86_64, b"", rodata, [(sym, 0, 1)])
        self.assertEqual(qd.cpp_qml_modules(elf), {"org.kde.newstuff"})

    def test_ambiguous_symbol_is_not_a_provide(self):
        rodata = b"org.kde.kwin_x11\0org.kde.kwin.x11\0"
        sym = itanium_register_types("org_kde_kwin_x11")
        elf = elf_with(EM_X86_64, b"", rodata, [(sym, 0, 1)])
        self.assertEqual(qd.cpp_qml_modules(elf), set())

    def test_undefined_symbol_is_not_a_provide(self):
        rodata = b"QtQuick\0"
        sym = itanium_register_types("QtQuick")
        elf = elf_with(EM_X86_64, b"", rodata, [(sym, 0, 0)])
        self.assertEqual(qd.cpp_qml_modules(elf), set())

    def test_marker_bytes_alone_are_not_a_provide(self):
        rodata = b"qml_register_types_org_kde_newstuff\0org.kde.newstuff\0"
        elf = elf_with(EM_X86_64, b"", rodata, [])
        self.assertEqual(qd.cpp_qml_modules(elf), set())

    def _protect_elf(self, text, rodata=b""):
        got = 0x4000
        plt = x86_plt(0, 0x3000, got)
        sym = "_Z16qmlProtectModulePKci"
        elf = elf_with(EM_X86_64, text, rodata, [(sym, 0, 0)], plt)
        return elf

    def test_uncreatable_metaobject_call_is_a_provide(self):
        # rsi, not rdi. The only registration marker in the file is this symbol.
        rodata = b"org.kde.kunifiedpush.kcm\0SelfTest\0"
        text = bytearray()
        at = 0x1000
        text += x86_lea_rip(at, 6, 0x2000)  # lea rsi
        at += 7
        text += b"\x90" * 40
        at += 40
        text += x86_call(at, 0x3000)
        plt = x86_plt(0, 0x3000, 0x4000)
        sym = "_Z32qmlRegisterUncreatableMetaObjectRK11QMetaObjectPKciiS3_RK7QString"
        elf = elf_with(EM_X86_64, bytes(text), rodata, [(sym, 0, 0)], plt)
        self.assertEqual(qd.cpp_qml_modules(elf), {"org.kde.kunifiedpush.kcm"})

    def test_register_module_call_ignores_other_strings(self):
        rodata = b"org.kde.example\0QtQuick\0"
        text = bytearray()
        at = 0x1000
        text += x86_lea_rip(at, 7, 0x2000)  # lea rdi, org.kde.example
        at += 7
        text += b"\x90" * 53  # lea starts 60 bytes before the call
        at += 53
        text += x86_call(at, 0x3000)
        # A following call through an unrelated import must not provide QtQuick.
        at += 5
        text += x86_lea_rip(at, 7, 0x2000 + len(b"org.kde.example\0"))
        at += 7
        text += x86_call(at, 0x3010)
        plt = x86_plt(0, 0x3000, 0x4000) + x86_plt(1, 0x3010, 0x4008)
        symbols = [("_Z17qmlRegisterModulePKcii", 0, 0), ("qmlRegisterModuleImport", 0, 0)]
        elf = elf_with(EM_X86_64, bytes(text), rodata, symbols, plt)
        self.assertEqual(qd.cpp_qml_modules(elf), {"org.kde.example"})

    def test_stack_array_immediate_is_a_provide(self):
        # clang -O2 for `const char uri[] = "SMART"; qmlProtectModule(uri, 1)`.
        text = (
            b"\x66\xc7\x44\x24\x04\x54\x00"  # movw $0x54, 4(%rsp)
            b"\xc7\x04\x24\x53\x4d\x41\x52"  # movl $0x52414d53, (%rsp)  "SMAR"
            b"\x48\x89\xe7"  # mov %rsp, %rdi
            b"\xbe\x01\x00\x00\x00"
        )
        at = 0x1000 + len(text)
        text += x86_call(at, 0x3000)
        elf = self._protect_elf(text, b"QtQuick\0")
        self.assertEqual(qd.cpp_qml_modules(elf), {"SMART"})

    def test_stack_array_filled_after_the_pointer(self):
        # gcc -O2 takes the address, then writes the immediates.
        text = (
            b"\x48\x8d\x7c\x24\x0a"  # lea 0xa(%rsp), %rdi
            b"\xc7\x44\x24\x0a\x53\x4d\x41\x52"
            b"\x66\xc7\x44\x24\x0e\x54\x00"
        )
        at = 0x1000 + len(text)
        text += x86_call(at, 0x3000)
        elf = self._protect_elf(text)
        self.assertEqual(qd.cpp_qml_modules(elf), {"SMART"})

    def test_stack_array_reassembled_from_chunks(self):
        # x86-64 gcc -O2 keeps no contiguous copy of a long literal. Two
        # 16-byte chunks are separated here by a non-string byte so reading
        # either chunk as a C string cannot see the module.
        raw = b"org.kde.private.kcms.notifications\0"
        rodata = raw[:16] + b"\xff" + raw[16:32]
        text = bytearray()
        cursor = 0x1000

        def emit(blob):
            nonlocal cursor
            text.extend(blob)
            cursor += len(blob)

        emit(b"\x0f\x28\x05" + struct.pack("<i", 0x2000 - (cursor + 7)))  # movaps chunk0
        emit(b"\x0f\x29\x04\x24")  # movaps (%rsp)
        emit(b"\x0f\x28\x05" + struct.pack("<i", 0x2011 - (cursor + 7)))  # chunk1
        emit(b"\x0f\x29\x44\x24\x10")
        emit(b"\xc7\x44\x24\x1f" + raw[31:35])
        emit(b"\x48\x89\xe7")
        emit(x86_call(cursor, 0x3000))
        elf = self._protect_elf(bytes(text), rodata)
        self.assertEqual(
            qd.cpp_qml_modules(elf),
            {"org.kde.private.kcms.notifications"},
        )

    def test_aligned_vex_copy_overlaps_on_the_stack(self):
        # clang -O2 for a longer `const char uri[]`. The aligned half is
        # vmovaps (opcode 28/29); the tail is an overlapping vmovups. Reading
        # either rodata chunk as a C string is not the module name.
        raw = b"org.kde.private.kcms.keys\0"
        text = bytearray()
        cursor = 0x1000

        def emit(blob):
            nonlocal cursor
            text.extend(blob)
            cursor += len(blob)

        def vex_rip(opcode, reg, target):
            disp = target - (cursor + 8)
            emit(bytes([0xC5, 0xF8, opcode, (reg << 3) | 5]) + struct.pack("<i", disp))

        def vex_rsp(opcode, reg, disp):
            emit(bytes([0xC5, 0xF8, opcode, 0x40 | (reg << 3) | 4, 0x24, disp & 0xFF]))

        vex_rip(0x10, 2, 0x2000 + 10)  # vmovups xmm2, [rip] tail
        vex_rip(0x28, 1, 0x2000)  # vmovaps xmm1, [rip] head
        vex_rsp(0x11, 2, 0x4A)
        vex_rsp(0x29, 1, 0x40)
        emit(b"\x48\x8d\x44\x24\x40")  # lea 0x40(%rsp), %rax
        emit(b"\x48\x89\x84\x24\xb0\x00\x00\x00")
        emit(b"\x66\xc7\x84\x24\xb8\x00\x00\x00\x00\x02")
        emit(x86_call(cursor, 0x3000))
        plt = x86_plt(0, 0x3000, 0x4000)
        elf = elf_with(
            EM_X86_64, bytes(text), raw,
            [("_ZN11QQmlPrivate11qmlregisterENS_16RegistrationTypeEPv", 0, 0)],
            plt,
        )
        self.assertEqual(qd.cpp_qml_modules(elf), {"org.kde.private.kcms.keys"})

    def test_struct_call_keeps_only_the_closest_store(self):
        rodata = b"org.kde.old\0org.kde.new\0"
        text = bytearray()
        cursor = 0x1000

        def emit(blob):
            nonlocal cursor
            text.extend(blob)
            cursor += len(blob)

        def store_rip(reg, uri):
            emit(x86_lea_rip(cursor, reg, uri))
            rex = 0x48 if reg < 8 else 0x4C
            reg3 = reg if reg < 8 else reg - 8
            emit(bytes([rex, 0x89, 0x40 | (reg3 << 3) | 4, 0x24, 0x50]))
            emit(b"\x66\xc7\x44\x24\x58\x00\x01")

        store_rip(3, 0x2000)  # rbx = org.kde.old
        store_rip(3, 0x2000 + len(b"org.kde.old\0"))
        emit(x86_call(cursor, 0x3000))
        plt = x86_plt(0, 0x3000, 0x4000)
        elf = elf_with(
            EM_X86_64, bytes(text), rodata,
            [("_ZN11QQmlPrivate11qmlregisterENS_16RegistrationTypeEPv", 0, 0)],
            plt,
        )
        self.assertEqual(qd.cpp_qml_modules(elf), {"org.kde.new"})

    def test_struct_call_reads_a_stack_array(self):
        rodata = b"Old.Mod\0"
        text = bytearray()
        cursor = 0x1000

        def emit(blob):
            nonlocal cursor
            text.extend(blob)
            cursor += len(blob)

        emit(x86_lea_rip(cursor, 3, 0x2000))  # lea rbx, Old.Mod
        emit(b"\x48\x89\x5c\x24\x50")
        emit(b"\x66\xc7\x44\x24\x58\x00\x01")
        emit(b"\xc7\x44\x24\x20\x4d\x6f\x64\x2e")  # "Mod."
        emit(b"\xc7\x44\x24\x24\x4e\x65\x77\x00")  # "New\0"
        emit(b"\x48\x8d\x54\x24\x20")  # lea rdx, 0x20(%rsp)
        emit(b"\x48\x89\x54\x24\x50")
        emit(b"\x66\xc7\x44\x24\x58\x00\x01")
        emit(x86_call(cursor, 0x3000))
        plt = x86_plt(0, 0x3000, 0x4000)
        elf = elf_with(
            EM_X86_64, bytes(text), rodata,
            [("_ZN11QQmlPrivate11qmlregisterENS_16RegistrationTypeEPv", 0, 0)],
            plt,
        )
        self.assertEqual(qd.cpp_qml_modules(elf), {"Mod.New"})

    def test_struct_addressed_through_lea_rsp(self):
        # clang -O2 fills QQmlPrivate::RegisterType through a pointer
        # register: lea r14, [rsp+0x70]; mov [r14+0x50], r15; movw [r14+0x58], 1.0
        rodata = b"org.kde.plasma.kcm.users\0FingerprintModel\0"
        text = bytearray()
        cursor = 0x1000

        def emit(blob):
            nonlocal cursor
            text.extend(blob)
            cursor += len(blob)

        emit(b"\x4c\x8d\x74\x24\x70")  # lea r14, [rsp+0x70]
        emit(x86_lea_rip(cursor, 15, 0x2000))
        emit(b"\x4d\x89\x7e\x50")  # mov [r14+0x50], r15
        emit(b"\x66\x41\xc7\x46\x58\x00\x01")  # movw [r14+0x58], 0x100
        emit(x86_call(cursor, 0x3000))
        emit(b"\x4c\x8d\x74\x24\x70")
        users = len(b"org.kde.plasma.kcm.users\0")
        emit(x86_lea_rip(cursor, 0, 0x2000 + users))
        emit(b"\x49\x89\x46\x50")  # mov [r14+0x50], rax
        emit(b"\x66\x41\xc7\x46\x58\x00\x01")
        emit(x86_call(cursor, 0x3000))
        plt = x86_plt(0, 0x3000, 0x4000)
        elf = elf_with(
            EM_X86_64, bytes(text), rodata,
            [("_ZN11QQmlPrivate11qmlregisterENS_16RegistrationTypeEPv", 0, 0)],
            plt,
        )
        self.assertEqual(
            qd.cpp_qml_modules(elf),
            {"org.kde.plasma.kcm.users", "FingerprintModel"},
        )

    def test_struct_base_is_rsp_copied_into_a_register(self):
        rodata = b"org.kde.private.kcms.nighttime\0"
        text = bytearray()
        cursor = 0x1000

        def emit(blob):
            nonlocal cursor
            text.extend(blob)
            cursor += len(blob)

        emit(b"\x49\x89\xe6")  # mov r14, rsp
        emit(x86_lea_rip(cursor, 15, 0x2000))
        emit(b"\x4d\x89\x7e\x50")
        # version in a register, not an immediate: mov %bp, [r14+0x58]
        emit(b"\x66\x41\x89\x6e\x58")
        emit(x86_call(cursor, 0x3000))
        plt = x86_plt(0, 0x3000, 0x4000)
        elf = elf_with(
            EM_X86_64, bytes(text), rodata,
            [("_ZN11QQmlPrivate11qmlregisterENS_16RegistrationTypeEPv", 0, 0)],
            plt,
        )
        self.assertEqual(
            qd.cpp_qml_modules(elf),
            {"org.kde.private.kcms.nighttime"},
        )

    def test_reused_base_register_is_not_a_frame_pointer(self):
        rodata = b"org.kde.plasma.kcm.users\0"
        text = bytearray()
        cursor = 0x1000

        def emit(blob):
            nonlocal cursor
            text.extend(blob)
            cursor += len(blob)

        emit(b"\x4c\x8d\x74\x24\x70")  # lea r14, [rsp+0x70]
        emit(b"\x4c\x8b\xf0")  # mov r14, rax; r14 no longer addresses the struct
        emit(x86_lea_rip(cursor, 15, 0x2000))
        emit(b"\x4d\x89\x7e\x50")
        emit(b"\x66\x41\xc7\x46\x58\x00\x01")
        emit(x86_call(cursor, 0x3000))
        plt = x86_plt(0, 0x3000, 0x4000)
        elf = elf_with(
            EM_X86_64, bytes(text), rodata,
            [("_ZN11QQmlPrivate11qmlregisterENS_16RegistrationTypeEPv", 0, 0)],
            plt,
        )
        self.assertEqual(qd.cpp_qml_modules(elf), set())

    def test_struct_uri_is_a_stack_array_addressed_by_a_register(self):
        # clang copies `const char uri[]` through `lea rax, [rsp+slot]`
        # and stores that pointer into RegisterType::uri.
        raw = b"org.kde.private.kcms.notifications\0"
        text = bytearray()
        cursor = 0x1000

        def emit(blob):
            nonlocal cursor
            text.extend(blob)
            cursor += len(blob)

        emit(b"\x0f\x28\x05" + struct.pack("<i", 0x2010 - (cursor + 7)))
        emit(b"\x48\x8d\x44\x24\x40")  # lea rax, [rsp+0x40]
        emit(b"\x0f\x29\x40\x10")  # movaps [rax+0x10], xmm0
        emit(b"\x0f\x28\x05" + struct.pack("<i", 0x2000 - (cursor + 7)))
        emit(b"\x0f\x29\x00")  # movaps [rax], xmm0
        emit(b"\xc7\x40\x1f" + raw[31:35])
        emit(b"\x4c\x8d\x74\x24\x70")  # lea r14, [rsp+0x70]
        emit(b"\x49\x89\x46\x50")  # mov [r14+0x50], rax
        emit(b"\x66\x41\xc7\x46\x58\x00\x01")
        emit(x86_call(cursor, 0x3000))
        plt = x86_plt(0, 0x3000, 0x4000)
        elf = elf_with(
            EM_X86_64, bytes(text), raw,
            [("_ZN11QQmlPrivate11qmlregisterENS_16RegistrationTypeEPv", 0, 0)],
            plt,
        )
        self.assertEqual(
            qd.cpp_qml_modules(elf),
            {"org.kde.private.kcms.notifications"},
        )

    def test_stack_array_terminated_by_movb_al(self):
        # 16 bytes of payload and a separate `movb %al, slot` after xorl %eax,%eax.
        # The rodata copy is not itself a C string, so the NUL has to be replayed.
        name = b"org.kde.exampleX"
        rodata = name + b"\xff"
        text = bytearray()
        cursor = 0x1000

        def emit(blob):
            nonlocal cursor
            text.extend(blob)
            cursor += len(blob)

        emit(b"\x31\xc0")
        emit(b"\x0f\x28\x05" + struct.pack("<i", 0x2000 - (cursor + 7)))
        emit(b"\x0f\x29\x44\x24\x20")  # movaps [rsp+0x20], xmm0
        emit(b"\x88\x44\x24\x30")  # movb %al, [rsp+0x30]
        emit(b"\x48\x8d\x7c\x24\x20")  # lea rdi, [rsp+0x20]
        emit(x86_call(cursor, 0x3000))
        elf = self._protect_elf(bytes(text), rodata)
        self.assertEqual(qd.cpp_qml_modules(elf), {"org.kde.exampleX"})

        # A write to eax between the xor and the movb means %al is not a NUL.
        text = bytearray()
        cursor = 0x1000

        def emit2(blob):
            nonlocal cursor
            text.extend(blob)
            cursor += len(blob)

        emit2(b"\x31\xc0")
        emit2(b"\xb8\x01\x00\x00\x00")
        emit2(b"\x0f\x28\x05" + struct.pack("<i", 0x2000 - (cursor + 7)))
        emit2(b"\x0f\x29\x44\x24\x20")
        emit2(b"\x88\x44\x24\x30")
        emit2(b"\x48\x8d\x7c\x24\x20")
        emit2(x86_call(cursor, 0x3000))
        elf = self._protect_elf(bytes(text), rodata)
        self.assertEqual(qd.cpp_qml_modules(elf), set())

    def test_metaobject_uri_copied_into_rsi(self):
        text = (
            b"\xc7\x44\x24\x10\x53\x4d\x41\x52"
            b"\x66\xc7\x44\x24\x14\x54\x00"
            b"\x48\x8d\x5c\x24\x10"  # lea rbx, [rsp+0x10]
            b"\x48\x89\xde"  # mov rsi, rbx
        )
        at = 0x1000 + len(text)
        text += x86_call(at, 0x3000)
        plt = x86_plt(0, 0x3000, 0x4000)
        sym = "_Z32qmlRegisterUncreatableMetaObjectRK11QMetaObjectPKciiS3_RK7QString"
        elf = elf_with(EM_X86_64, text, b"QtQuick\0", [(sym, 0, 0)], plt)
        self.assertEqual(qd.cpp_qml_modules(elf), {"SMART"})

    def test_requires_skip_cpp_modules(self):
        text = (
            b"\x66\xc7\x44\x24\x04\x54\x00"
            b"\xc7\x04\x24\x53\x4d\x41\x52"
            b"\x48\x89\xe7"
            b"\xbe\x01\x00\x00\x00"
        )
        text += x86_call(0x1000 + len(text), 0x3000)
        elf = self._protect_elf(text)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "plugin.so")
            with open(path, "wb") as fh:
                fh.write(elf)
            self.assertEqual(qd.deps_for_path(path, "provides"), ["qml(SMART)"])
            self.assertEqual(qd.deps_for_path(path, "requires"), [])
            os.environ["RPM_QML_REQUIRES"] = "0"
            try:
                self.assertEqual(qd.deps_for_path(path, "provides"), ["qml(SMART)"])
            finally:
                os.environ.pop("RPM_QML_REQUIRES", None)

    def test_truncated_elf_does_not_raise(self):
        data = b"\x7fELF\x02\x01" + b"\x00" * 20 + b"qml_register_types_"
        self.assertEqual(qd.cpp_qml_modules(data), set())

    def test_aarch64_stack_array_and_literal(self):
        # clang -O2, shortened to the instructions the scanner understands.
        words = [
            0x5289AA69,  # mov w9, #0x4d53
            0x72AA4829,  # movk w9, #0x5241, lsl #16
            0x52800A88,  # mov w8, #0x54
            0x910023E0,  # add x0, sp, #8
            0x79001BE8,  # strh w8, [sp, #12]
            0xB9000BE9,  # str w9, [sp, #8]
        ]
        bl_at = 0x1000 + 4 * len(words)
        words.append(0x94000000 | (((0x3000 - bl_at) >> 2) & 0x03FFFFFF))
        text = b"".join(word.to_bytes(4, "little") for word in words)
        plt = (0x90000010).to_bytes(4, "little")  # adrp x16, page
        plt += (0xF9408211).to_bytes(4, "little")  # ldr x17, [x16, #0x100]
        plt += (0xD61F0220).to_bytes(4, "little")  # br x17
        plt += (0xD503201F).to_bytes(4, "little")
        # GOT used by this stub is 0x3100, not the x86 default. Build directly.
        elf = build_elf(
            EM_AARCH64,
            [
                (".text", SHT_PROGBITS, 6, 0x1000, 0, text),
                (".plt", SHT_PROGBITS, 6, 0x3000, 0, plt),
            ],
            [("_Z16qmlProtectModulePKci", 0, 0)],
            [(0x3100, 1)],
        )
        self.assertEqual(qd.cpp_qml_modules(elf), {"SMART"})

        raw = b"org.kde.example\0QtQuick\0"
        # adrp x0, 0x2000; add x0, x0, #0; bl protect. pc of adrp is 0x1000.
        adrp = 0xB0000000  # imm 1, x0
        add = 0x91000000  # add x0, x0, #0
        bl = 0x94000000 | (((0x3000 - 0x1008) >> 2) & 0x03FFFFFF)
        text = b"".join(word.to_bytes(4, "little") for word in (adrp, add, bl))
        elf = build_elf(
            EM_AARCH64,
            [
                (".text", SHT_PROGBITS, 6, 0x1000, 0, text),
                (".rodata", SHT_PROGBITS, 2, 0x2000, 0, raw),
                (".plt", SHT_PROGBITS, 6, 0x3000, 0, plt),
            ],
            [("_Z17qmlRegisterModulePKcii", 0, 0)],
            [(0x3100, 1)],
        )
        self.assertEqual(qd.cpp_qml_modules(elf), {"org.kde.example"})

    def test_aarch64_struct_addressed_through_add_sp(self):
        raw = b"org.kde.plasma.kcm.users\0"
        words = [
            0x9101C3E8,  # add x8, sp, #0x70
            0xB0000001,  # adrp x1, 0x2000 (pc page is 0x1000)
            0x91000021,  # add x1, x1, #0
            0xF9002901,  # str x1, [x8, #0x50]
            0x7900B102,  # strh w2, [x8, #0x58]
        ]
        bl_at = 0x1000 + 4 * len(words)
        words.append(0x94000000 | (((0x3000 - bl_at) >> 2) & 0x03FFFFFF))
        text = b"".join(word.to_bytes(4, "little") for word in words)
        plt = (0x90000010).to_bytes(4, "little")
        plt += (0xF9408211).to_bytes(4, "little")
        plt += (0xD61F0220).to_bytes(4, "little")
        plt += (0xD503201F).to_bytes(4, "little")
        elf = build_elf(
            EM_AARCH64,
            [
                (".text", SHT_PROGBITS, 6, 0x1000, 0, text),
                (".rodata", SHT_PROGBITS, 2, 0x2000, 0, raw),
                (".plt", SHT_PROGBITS, 6, 0x3000, 0, plt),
            ],
            [("_ZN11QQmlPrivate11qmlregisterENS_16RegistrationTypeEPv", 0, 0)],
            [(0x3100, 1)],
        )
        self.assertEqual(qd.cpp_qml_modules(elf), {"org.kde.plasma.kcm.users"})

    def test_aarch64_struct_uri_copied_with_q_register(self):
        # 16 payload bytes, then strb wzr. The rodata is not a C string.
        raw = b"org.kde.exampleX\xff"
        words = [
            0x910103E8,  # add x8, sp, #0x40
            0xB0000001,  # adrp x1, 0x2000
            0x91000021,  # add x1, x1, #0
            0x3DC00020,  # ldr q0, [x1]
            0x3D800100,  # str q0, [x8]
            0x3900411F,  # strb wzr, [x8, #16]
            0x9101C3E9,  # add x9, sp, #0x70
            0xF9002928,  # str x8, [x9, #0x50]
            0x7900B122,  # strh w2, [x9, #0x58]
        ]
        bl_at = 0x1000 + 4 * len(words)
        words.append(0x94000000 | (((0x3000 - bl_at) >> 2) & 0x03FFFFFF))
        text = b"".join(word.to_bytes(4, "little") for word in words)
        plt = (0x90000010).to_bytes(4, "little")
        plt += (0xF9408211).to_bytes(4, "little")
        plt += (0xD61F0220).to_bytes(4, "little")
        plt += (0xD503201F).to_bytes(4, "little")
        elf = build_elf(
            EM_AARCH64,
            [
                (".text", SHT_PROGBITS, 6, 0x1000, 0, text),
                (".rodata", SHT_PROGBITS, 2, 0x2000, 0, raw),
                (".plt", SHT_PROGBITS, 6, 0x3000, 0, plt),
            ],
            [("_ZN11QQmlPrivate11qmlregisterENS_16RegistrationTypeEPv", 0, 0)],
            [(0x3100, 1)],
        )
        self.assertEqual(qd.cpp_qml_modules(elf), {"org.kde.exampleX"})


class InstalledCppTests(unittest.TestCase):
    def test_notifications_provides_the_module_it_imports(self):
        path = "/usr/lib64/qt6/plugins/plasma/kcms/systemsettings/kcm_notifications.so"
        if not os.path.exists(path):
            self.skipTest("notifications kcm not installed")
        provides = set(qd.deps_for_path(path, "provides"))
        requires = set(qd.deps_for_path(path, "requires"))
        module = "qml(org.kde.private.kcms.notifications)"
        self.assertIn(module, requires)
        self.assertIn(module, provides)
        for extra in ("QKeySequence", "BadgeSettings", "FilterProxyModel"):
            self.assertIn("qml(" + extra + ")", provides)

    def test_cpp_modules_match_their_qml_imports(self):
        cases = [
            (
                "/usr/lib64/qt6/plugins/plasma/kcms/kinfocenter/kcm_energyinfo.so",
                "org.kde.kinfocenter.energy.private",
            ),
            (
                "/usr/lib64/qt6/plugins/plasma/kcms/kinfocenter/kcm_samba.so",
                "org.kde.kinfocenter.samba",
            ),
            (
                "/usr/lib64/qt6/plugins/plasma/kcms/kinfocenter/kcm_disks.so",
                "SMART",
            ),
            (
                "/usr/lib64/qt6/plugins/plasma/kcms/kcm_about-distro.so",
                "org.kde.kinfocenter.about_distro.private",
            ),
        ]
        for path, module in cases:
            if not os.path.exists(path):
                self.skipTest(path + " not installed")
            name = "qml(" + module + ")"
            self.assertIn(name, qd.deps_for_path(path, "provides"), path)
            self.assertIn(name, qd.deps_for_path(path, "requires"), path)
        for path, module in (
            (
                "/usr/lib64/qt6/plugins/plasma/kcms/systemsettings/kcm_push_notifications.so",
                "org.kde.kunifiedpush.kcm",
            ),
            (
                "/usr/lib64/qt6/plugins/plasma/kcms/systemsettings/kcm_powerdevilprofilesconfig.so",
                "org.kde.powerdevil",
            ),
        ):
            if os.path.exists(path):
                name = "qml(" + module + ")"
                self.assertIn(name, qd.deps_for_path(path, "requires"), path)
                self.assertIn(name, qd.deps_for_path(path, "provides"), path)
        keys = "/usr/lib64/qt6/plugins/plasma/kcms/systemsettings/kcm_keys.so"
        if os.path.exists(keys):
            name = "qml(org.kde.private.kcms.keys)"
            self.assertIn(name, qd.deps_for_path(keys, "requires"))
            self.assertIn(name, qd.deps_for_path(keys, "provides"))
        about = "/usr/lib64/qt6/plugins/plasma/kcms/kcm_about-distro.so"
        if os.path.exists(about):
            self.assertNotIn(
                "qml(org.kde.kinfocenter)",
                qd.deps_for_path(about, "provides"),
            )


if __name__ == "__main__":
    unittest.main()
