#!/usr/bin/python
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the QML Provides/Requires generator."""

import base64
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

    def test_lowercase_helper_module_is_not_a_require(self):
        # Qt examples: import shared as Examples. The helper plugin is not
        # installed in a prefix build, so qml(shared) has no provider.
        text = """\
import QtQuick
import shared as Examples
import fbitem
import chattutorial
import org.kde.plasma.core
"""
        self.assertEqual(
            qd.qml_imports(text),
            {
                "QtQuick",
                "shared",
                "fbitem",
                "chattutorial",
                "org.kde.plasma.core",
            },
        )
        self.assertEqual(
            qd.deps_from_texts([("animation.qml", text)], "requires"),
            ["qml(QtQuick)", "qml(org.kde.plasma.core)"],
        )
        qmldir = "module shared\nLauncherList 2.0 LauncherList.qml\ndepends extrahelpers\n"
        provides, requires = qd.parse_qmldir(qmldir)
        self.assertIn("qml(shared) = 2.0", provides)
        self.assertEqual(requires, {"extrahelpers"})
        self.assertEqual(qd.deps_from_texts([("qmldir", qmldir)], "provides"), [
            "qml(shared) = 2.0",
            "qml(shared.LauncherList) = 2.0",
        ])
        self.assertEqual(qd.deps_from_texts([("qmldir", qmldir)], "requires"), [])
        self.assertTrue(qd.external_qml_module("QtQuick"))
        self.assertTrue(qd.external_qml_module("SddmComponents"))
        self.assertTrue(qd.external_qml_module("org.kde.kirigami"))
        self.assertFalse(qd.external_qml_module("shared"))

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
EM_RISCV = 243
EM_LOONGARCH = 258
EM_PPC64 = 21


def _strtab(strings):
    blob = bytearray(b"\0")
    offsets = {}
    for text in strings:
        if text in offsets:
            continue
        offsets[text] = len(blob)
        blob += text.encode() + b"\0"
    return bytes(blob), offsets


def build_elf(
    machine, parts, symbols, relas=None, relatives=None, endian="little", relative_type=3
):
    """Tiny ELF64 shared object.

    parts: (name, type, flags, addr, entsize, data)
    symbols: (name, value, shndx) after the null symbol. shndx 0 is undefined.
    relas: (got_addr, symbol_index) with symbol_index 1-based.
    relatives: (vaddr, addend) R_*_RELATIVE entries. Type 3, or 22 on ppc64.
    endian: "little" or "big". ppc64 is big-endian; ppc64le is little-endian.
    """
    e = "<" if endian == "little" else ">"
    dynstr, dynoff = _strtab([name for name, _value, _shndx in symbols])
    dynsym = [struct.pack(e + "IBBHQQ", 0, 0, 0, 0, 0, 0)]
    for name, value, shndx in symbols:
        info = 0x12 if shndx else 0x10
        dynsym.append(struct.pack(e + "IBBHQQ", dynoff[name], info, 0, shndx, value, 0))
    rela = b""
    for got, symi in relas or ():
        rela += struct.pack(e + "QQq", got, (symi << 32) | 7, 0)
    items = [("", 0, 0, 0, 0, b"")]
    items.extend(parts)
    items.append((".dynstr", SHT_STRTAB, 0, 0, 0, dynstr))
    items.append((".dynsym", SHT_DYNSYM, 0, 0, 24, b"".join(dynsym)))
    if relas:
        items.append((".rela.plt", SHT_RELA, 0, 0, 24, rela))
    if relatives:
        rela_dyn = b"".join(
            struct.pack(e + "QQq", off, relative_type, addend) for off, addend in relatives
        )
        items.append((".rela.dyn", SHT_RELA, 0, 0, 24, rela_dyn))
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
            e + "IIQQQQIIQQ", nameoff[name], typ, flags, addr, off, len(data), 0, 0, 1, entsize
        )
    struct.pack_into(
        e + "HHIQQQIHHHHHH", blob, 16,
        3, machine, 1, 0, 0, shoff, 0, 64, 0, 0, 64, len(items), len(items) - 1,
    )
    blob[0:4] = b"\x7fELF"
    blob[4] = 2
    blob[5] = 1 if endian == "little" else 2
    blob[6] = 1
    return bytes(blob)


def aarch64_adr(rd, pc, target):
    imm = target - pc
    if not -(1 << 20) <= imm < (1 << 20):
        raise ValueError(hex(imm))
    if imm < 0:
        imm += 1 << 21
    immlo = imm & 3
    immhi = (imm >> 2) & 0x7FFFF
    return (immlo << 29) | 0x10000000 | (immhi << 5) | rd


def aarch64_add(rd, rn, imm):
    return 0x91000000 | ((imm & 0xFFF) << 10) | (rn << 5) | rd


def aarch64_str64(rt, rn, disp):
    return 0xF9000000 | ((disp // 8) << 10) | (rn << 5) | rt


def aarch64_strh(rt, rn, disp):
    return 0x79000000 | ((disp // 2) << 10) | (rn << 5) | rt


def aarch64_stp64(rt, rt2, rn, disp):
    return 0xA9000000 | (((disp // 8) & 0x7F) << 15) | (rt2 << 10) | (rn << 5) | rt


def aarch64_ldr_q(rt, rn, disp=0):
    return 0x3DC00000 | ((disp // 16) << 10) | (rn << 5) | rt


def aarch64_str_q(rt, rn, disp=0):
    return 0x3D800000 | ((disp // 16) << 10) | (rn << 5) | rt


def aarch64_b(byte_disp):
    return 0x14000000 | ((byte_disp // 4) & 0x03FFFFFF)


def aarch64_bl(pc, target=0x3000):
    return 0x94000000 | (((target - pc) >> 2) & 0x03FFFFFF)


def aarch64_register_elf(words, rodata=b""):
    """ELF whose last instruction is BL to a qmlregister PLT stub."""
    bl_at = 0x1000 + 4 * len(words)
    text_words = list(words) + [aarch64_bl(bl_at)]
    text = b"".join(word.to_bytes(4, "little") for word in text_words)
    plt = b"".join(word.to_bytes(4, "little") for word in (
        0x90000010, 0xF9408211, 0xD61F0220, 0xD503201F,
    ))
    parts = [(".text", SHT_PROGBITS, 6, 0x1000, 0, text)]
    if rodata:
        parts.append((".rodata", SHT_PROGBITS, 2, 0x2000, 0, rodata))
    parts.append((".plt", SHT_PROGBITS, 6, 0x3000, 0, plt))
    return build_elf(
        EM_AARCH64,
        parts,
        [("_ZN11QQmlPrivate11qmlregisterENS_16RegistrationTypeEPv", 0, 0)],
        [(0x3100, 1)],
    )


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

    def test_rexw_store_through_r12_does_not_clobber_stack_uri(self):
        # kcm_keys on x86_64 copies the URI with overlapping movups/movaps,
        # then zeroes a field of a struct at [rsp+0x60] with
        # `mov qword ptr [r12+0x48], 0`. The REX.W prefix selects r12. Without
        # it, that instruction is `mov dword ptr [rsp+0x48], 0` and splits the URI.
        raw = b"org.kde.private.kcms.keys\0"
        text = bytearray()
        cursor = 0x1000

        def emit(blob):
            nonlocal cursor
            text.extend(blob)
            cursor += len(blob)

        def sse_rip(opcode, target):
            disp = target - (cursor + 7)
            emit(bytes([0x0F, opcode, 0x05]) + struct.pack("<i", disp))

        def sse_rsp(opcode, disp):
            emit(bytes([0x0F, opcode, 0x44, 0x24, disp & 0xFF]))

        sse_rip(0x10, 0x2000 + 10)  # movups xmm0, tail
        sse_rsp(0x11, 0x4A)
        sse_rip(0x28, 0x2000)  # movaps xmm0, head
        sse_rsp(0x29, 0x40)
        emit(b"\x4c\x8d\x64\x24\x60")  # lea r12, [rsp+0x60]
        emit(b"\x49\xc7\x44\x24\x48\x00\x00\x00\x00")  # mov qword [r12+0x48], 0
        emit(b"\x48\x8d\x7c\x24\x40")  # lea rdi, [rsp+0x40]
        emit(b"\xbe\x02\x00\x00\x00")
        emit(x86_call(cursor, 0x3000))
        elf = self._protect_elf(bytes(text), raw)
        self.assertEqual(qd.cpp_qml_modules(elf), {"org.kde.private.kcms.keys"})

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

    def test_aarch64_adr_overrides_earlier_page_address(self):
        # feedback: adrp+add leaves a vtable in x8, then adr loads the URI.
        raw = b"org.kde.decoy\0org.kde.kept\0"
        kept = 0x2000 + raw.index(b"org.kde.kept")
        adr_at = 0x1008
        words = [
            0xB0000008,  # adrp x8, 0x2000
            aarch64_add(8, 8, 0),
            aarch64_adr(8, adr_at, kept),
            aarch64_str64(8, 31, 0x50),
            aarch64_strh(2, 31, 0x58),
        ]
        self.assertEqual(qd._aarch64_adr(words[2], adr_at), (8, kept))
        elf = aarch64_register_elf(words, raw)
        self.assertEqual(qd.cpp_qml_modules(elf), {"org.kde.kept"})

    def test_aarch64_struct_uri_stored_with_stp(self):
        # autostart: `stp xzr, x20, [sp, #imm]` puts the URI at disp+8.
        raw = b"org.kde.plasma.kcm.autostart\0"
        words = [
            0xB0000014,  # adrp x20, 0x2000
            aarch64_add(20, 20, 0),
            aarch64_stp64(31, 20, 31, 0x48),
            aarch64_strh(2, 31, 0x58),
        ]
        self.assertEqual(qd._aarch64_stp64(words[2]), (31, 20, 31, 0x48))
        elf = aarch64_register_elf(words, raw)
        self.assertEqual(qd.cpp_qml_modules(elf), {"org.kde.plasma.kcm.autostart"})

    def test_aarch64_adr_uri_stored_with_stp(self):
        raw = b"org.kde.plasma.kcm.users\0"
        words = [
            aarch64_adr(21, 0x1000, 0x2000),
            aarch64_stp64(31, 21, 31, 0x48),
            aarch64_strh(2, 31, 0x58),
        ]
        elf = aarch64_register_elf(words, raw)
        self.assertEqual(qd.cpp_qml_modules(elf), {"org.kde.plasma.kcm.users"})

    def test_aarch64_stp_pairs_with_strh_52_bytes_later(self):
        # cursortheme: 13 instructions, 52 bytes, between the stp and the strh.
        raw = b"org.kde.private.kcm_cursortheme\0"
        words = [
            0xB0000008,  # adrp x8, 0x2000
            aarch64_add(8, 8, 0),
            aarch64_stp64(31, 8, 31, 0x48),
        ]
        words.extend([0xD503201F] * 12)  # nop
        words.append(aarch64_strh(2, 31, 0x58))
        self.assertEqual(4 * (len(words) - 1 - 2), 52)
        elf = aarch64_register_elf(words, raw)
        self.assertEqual(qd.cpp_qml_modules(elf), {"org.kde.private.kcm_cursortheme"})

    def test_aarch64_callee_saved_slot_survives_short_branch(self):
        # lookandfeel: `add x22, sp, #n` before a cbz/b diamond and a BL,
        # then the URI is copied through x22. The rodata has no NUL, so a
        # missed stack image cannot fall back to a C string search.
        raw = b"org.kde.exampleX" + b"\xff" * 16
        words = [
            aarch64_add(22, 31, 0x40),
            aarch64_b(8),
            0xD503201F,  # nop, the other arm of the diamond
            aarch64_bl(0x100c),
            0xB0000009,  # adrp x9, 0x2000
            aarch64_add(9, 9, 0),
            aarch64_ldr_q(0, 9),
            aarch64_str_q(0, 22),
            0x39000000 | (0x50 << 10) | (31 << 5) | 31,  # strb wzr, [sp, #0x50]
            aarch64_str64(22, 31, 0x90),
            aarch64_strh(2, 31, 0x98),
        ]
        elf = aarch64_register_elf(words, raw)
        self.assertEqual(qd.cpp_qml_modules(elf), {"org.kde.exampleX"})

    def test_aarch64_long_branch_drops_callee_saved_slot(self):
        raw = b"org.kde.exampleX" + b"\xff" * 16
        words = [
            aarch64_add(22, 31, 0x40),
            aarch64_b(0x100),
            0xB0000009,
            aarch64_add(9, 9, 0),
            aarch64_ldr_q(0, 9),
            aarch64_str_q(0, 22),
            0x39000000 | (0x50 << 10) | (31 << 5) | 31,
            aarch64_str64(22, 31, 0x90),
            aarch64_strh(2, 31, 0x98),
        ]
        elf = aarch64_register_elf(words, raw)
        self.assertEqual(qd.cpp_qml_modules(elf), set())


QMLREGISTER = "_ZN11QQmlPrivate11qmlregisterENS_16RegistrationTypeEPv"
QMLPROTECT = "_Z16qmlProtectModulePKci"
QMLUNCREATABLE = (
    "_Z32qmlRegisterUncreatableMetaObjectRK11QMetaObjectPKciiS3_RK7QString"
)


def u32s(*words):
    return b"".join((word & 0xFFFFFFFF).to_bytes(4, "little") for word in words)


def rv_auipc(rd, imm20):
    return ((imm20 & 0xFFFFF) << 12) | (rd << 7) | 0x17


def rv_addi(rd, rs, imm):
    return ((imm & 0xFFF) << 20) | (rs << 15) | (rd << 7) | 0x13


def rv_jal(rd, disp):
    word = (rd << 7) | 0x6F
    word |= ((disp >> 20) & 1) << 31
    word |= ((disp >> 1) & 0x3FF) << 21
    word |= ((disp >> 11) & 1) << 20
    word |= ((disp >> 12) & 0xFF) << 12
    return word


def rv_ld(rd, rs, imm):
    return ((imm & 0xFFF) << 20) | (rs << 15) | (3 << 12) | (rd << 7) | 0x03


def rv_store(funct3, rs2, rs1, imm):
    imm &= 0xFFF
    return (
        ((imm >> 5) << 25)
        | (rs2 << 20)
        | (rs1 << 15)
        | (funct3 << 12)
        | ((imm & 0x1F) << 7)
        | 0x23
    )


def rv_plt(stub, got):
    delta = got - stub
    hi, lo = delta >> 12, delta & 0xFFF
    return u32s(rv_auipc(28, hi), rv_ld(28, 28, lo), (28 << 15) | (6 << 7) | 0x67, 0x13)


def la_2ri12(op, si12, rj, rd):
    return (op << 22) | ((si12 & 0xFFF) << 10) | (rj << 5) | rd


def la_pcrel(op7, si20, rd):
    return (op7 << 25) | ((si20 & 0xFFFFF) << 5) | rd


def la_bl(disp):
    imm = (disp >> 2) & 0x3FFFFFF
    low = ((imm & 0xFFFF) << 10) | ((imm >> 16) & 0x3FF)
    return (0x15 << 26) | low


def la_plt(stub, got):
    delta = got - stub
    si20, si12 = delta >> 12, delta & 0xFFF
    return u32s(
        la_pcrel(0x0E, si20, 15),
        la_2ri12(0x0A3, si12, 15, 15),
        la_2ri12(0x130, 0, 15, 13),
        0x03400000,
    )


def port_elf(machine, text, rodata, plt, symbols, relatives=None, extra=()):
    """ELF with one 16-byte PLT stub per symbol. GOT slots start at 0x3100."""
    parts = [(".text", SHT_PROGBITS, 6, 0x1000, 0, text)]
    if rodata:
        parts.append((".rodata", SHT_PROGBITS, 2, 0x2000, 0, rodata))
    parts.extend(extra)
    parts.append((".plt", SHT_PROGBITS, 6, 0x3000, 0, plt))
    relas = [(0x3100 + 8 * i, i + 1) for i in range(len(symbols))]
    return build_elf(machine, parts, [(name, 0, 0) for name in symbols], relas, relatives)


def _unpack_fixture(text):
    raw = zlib.decompress(base64.b64decode(text))
    out = {}
    pos = 0
    for name in (".text", ".rodata", ".data.rel.ro", ".plt"):
        addr, size = struct.unpack_from("<QI", raw, pos)
        pos += 12
        out[name] = (addr, raw[pos:pos + size])
        pos += size
    return out


# Clang -O2 -fPIC -shared -nostdlib output for the registration fixture.
# Text, rodata, the relocated RegisterType image and the PLT, at the
# original virtual addresses. Compressed RISC-V and the LoongArch vector
# copy are in here; the synthetic tests above use only the 32-bit forms.
_RV_FIXTURE = (
    "eNrFUEFr02AYftMsWZFaM7OS7TaMSi36HSrrURBJPYiClB2FZu3XEpombZOJu60t7UGkCD30OgR38NCDOI8y"
    "UX+CF9mhG+2oUO0OUxQc3ZtmLSM38eAHDx/P+zzf877f+0yC8VllAFaK9V4rBNCscUt+QeQ3zg1hSQoIjDKETUlC"
    "ReR64YAq0HnNnyedaDfWjwye1sZ6SPoxGonc50EN2c4p632tKUx8wqWfo1GT4w7E2YMBp3Dxcrx8dwgQaic1tbIR"
    "Kta/Oa7WqbOFYGNbnerbzB67/HK/ut3bl/1MIPY7etzee9ULgzNhGbt15s4rgUWRByHysd2R59/PXAdnpmTge/IG"
    "Jj8q1rtnk6vbn76wMX6XXeZ2o0fkV+SPDMwM+oP43wthocmB4OtG+mQgC4xPnkNcRIiMz83t8GHB+WVydiupYv56"
    "kT9yt8MtTrfTjfZjg8ih+2KHfVFQ0LnAu9t+g1izaClDabpQ0h6rNiWraipHcqm8WcqSXJqSnGqkUdexZBHdNCf1g"
    "q5aedVxEifCggemrWW0lGprppGgtq0ZWQsmbvpEzRd0CnGs0hL2Muz7ZprqU8Ok/biNcSbKghWMh4f36HqCFteok"
    "aLw/Jo7fx/Bwj8eBv7PYf5OP1xw701ESwa4yX64zb6rXhXZO8fNCt4JEaqvK5CFIEgE93KrcSXLBkFEv8uDHt647"
    "NE9vCF7dA9vXPLoU34Cs8nqCw=="
)
_LA_FIXTURE = (
    "eNrFUr+L1EAUfjPJrisc/ijEn0UOLBII+zfMKYrgKRwXwcIiYzJ7F3Y3WTc50W5OduWKK/0Dst0Vbm8jzHrN"
    "LRzYqYXFihY2YiOoILfOJPvjDNeIhQ8ek2++773v8Sa8jEDFC3l4/Bem7q51GcOFLv+IfSFwOX2iwR5ymPzW4QzA"
    "PnL0s5VznIC9CWC7nNiUEJuRCf9c8gfji533b3FJ59oM381xWWl28rtu+garc2v9HdYVJ/OY5I8rzTPkyFlMj+9i"
    "A2AZgGgqZzP+/H6+y8GczsGIZrlw1abiiu0b+dwl1eee9Frgmm9wyxevsA8DS4eOxEsWMyYz/4AjvWae4gBTY2B1"
    "vklPF8ynRJhceQKyaiNhuSmxQUcOnEKOawjLTwX2ich9XGK561LDxdxvCxylyfazLb2NgemRQebtkTGmqbA+LRza"
    "sSt3bMgdT+tvyJpUmJ58E1WzU8qeEIYyo/Zate6zaqtB4yat1r1mdSNm7RhuR0lQCzyaBFG4ypIkCNfimZo9os1W"
    "g8F1ecvarXYQJrcinzXm7drBQ5ow1S+uhodaxXBHtoeVm+zxKnuwwUKPZbNMC5V5jTH/PvXqqhomcWTjRhTVaehL"
    "fQNGFZwJP8vU4B8Dwf8J9Hc8qeQXqcwTOlzqvzwNr3tDs78yxvu9Ie6XrsH2ojBHCJZPSv4rHZpfUP7DZnipgBcL"
    "GBXwh70/8aCAewW8Oce/AR1w8fs="
)


class RiscvLoongarchTests(unittest.TestCase):
    """Call scanners for the in-progress riscv64 and loongarch64 ports."""

    def test_branch_immediates_roundtrip(self):
        self.assertEqual(qd._rv_jal_imm(rv_jal(1, 0x172)), 0x172)
        self.assertEqual(qd._rv_jal_imm(rv_jal(1, -8)), -8)
        self.assertEqual(qd._larch_i26(la_bl(456)), 456)
        self.assertEqual(qd._larch_i26(la_bl(-456)), -456)
        self.assertEqual(qd._larch_i26(la_bl(131072)), 131072)
        self.assertEqual(qd._larch_i26(la_bl(-131072)), -131072)

    def test_riscv_protect_module_from_auipc(self):
        text = u32s(rv_auipc(10, 1), rv_addi(10, 10, 0), rv_jal(1, 0x3000 - 0x1008))
        elf = port_elf(EM_RISCV, text, b"org.kde.example\0", rv_plt(0x3000, 0x3100), [QMLPROTECT])
        self.assertEqual(qd.cpp_qml_modules(elf), {"org.kde.example"})

    def test_riscv_stack_uri_is_captured_before_the_register_is_reused(self):
        text = u32s(
            rv_auipc(10, 1),
            rv_addi(10, 10, 0),
            rv_ld(11, 10, 0),
            rv_ld(12, 10, 8),
            rv_store(3, 11, 2, 0x20),
            rv_store(3, 12, 2, 0x28),
            rv_addi(11, 2, 0x20),
            rv_store(3, 11, 2, 0x40),
            rv_addi(12, 0, 0x100),
            rv_store(1, 12, 2, 0x48),
            rv_addi(11, 2, 8),
            rv_addi(10, 0, 0),
            rv_jal(1, 0x3000 - 0x1030),
        )
        elf = port_elf(
            EM_RISCV, text, b"org.kde.example\0", rv_plt(0x3000, 0x3100), [QMLREGISTER]
        )
        self.assertEqual(qd.cpp_qml_modules(elf), {"org.kde.example"})

    def test_riscv_uncreatable_reads_the_uri_not_the_type_name(self):
        text = u32s(
            rv_auipc(10, 1),
            rv_addi(11, 10, 0),
            rv_addi(14, 10, 25),
            rv_addi(10, 0, 1),
            rv_jal(1, 0x3000 - 0x1010),
        )
        ro = b"org.kde.plasma.kcm.users\0User\0"
        elf = port_elf(EM_RISCV, text, ro, rv_plt(0x3000, 0x3100), [QMLUNCREATABLE])
        self.assertEqual(qd.cpp_qml_modules(elf), {"org.kde.plasma.kcm.users"})

    def test_riscv_memcpy_image_skips_a_metaobject_name(self):
        note = b"org.kde.private.kcms.notifications\0"
        meta = b"NotificationSettings\0"
        image = bytearray(0x60)
        image[0:4] = (3).to_bytes(4, "little")
        image[0x40:0x48] = (0x100).to_bytes(8, "little")
        text = u32s(
            rv_auipc(11, 3),
            rv_addi(11, 11, 0),
            rv_addi(10, 2, 0x20),
            rv_addi(12, 0, 0x60),
            rv_jal(1, 0x3000 - 0x1010),
            rv_addi(11, 2, 0x20),
            rv_addi(10, 0, 0),
            rv_jal(1, 0x3010 - 0x101C),
        )
        plt = rv_plt(0x3000, 0x3100) + rv_plt(0x3010, 0x3108)
        elf = port_elf(
            EM_RISCV,
            text,
            note + meta,
            plt,
            ["memcpy", QMLREGISTER],
            [(0x4038, 0x2000), (0x4048, 0x2000 + len(note))],
            extra=[(".data.rel.ro", SHT_PROGBITS, 2, 0x4000, 0, bytes(image))],
        )
        self.assertEqual(qd.cpp_qml_modules(elf), {"org.kde.private.kcms.notifications"})

    def test_loongarch_protect_module_from_pcalau12i(self):
        text = u32s(la_pcrel(0x0D, 1, 4), la_2ri12(0x00B, 0, 4, 4), la_bl(0x3000 - 0x1008))
        elf = port_elf(EM_LOONGARCH, text, b"org.kde.example\0", la_plt(0x3000, 0x3100), [QMLPROTECT])
        self.assertEqual(qd.cpp_qml_modules(elf), {"org.kde.example"})

    def test_loongarch_stack_uri_paired_with_sth(self):
        text = u32s(
            la_pcrel(0x0D, 1, 4),
            la_2ri12(0x00B, 0, 4, 4),
            la_2ri12(0x0A3, 0, 4, 5),
            la_2ri12(0x0A3, 8, 4, 6),
            la_2ri12(0x0A7, 0x20, 3, 5),
            la_2ri12(0x0A7, 0x28, 3, 6),
            la_2ri12(0x00B, 0x20, 3, 4),
            la_2ri12(0x0A7, 0x40, 3, 4),
            la_2ri12(0x00E, 0x100, 0, 4),
            la_2ri12(0x0A5, 0x48, 3, 4),
            la_2ri12(0x00B, 8, 3, 5),
            (0x2A << 15) | 4,
            la_bl(0x3000 - 0x1030),
        )
        elf = port_elf(
            EM_LOONGARCH, text, b"org.kde.example\0", la_plt(0x3000, 0x3100), [QMLREGISTER]
        )
        self.assertEqual(qd.cpp_qml_modules(elf), {"org.kde.example"})

    def test_loongarch_uncreatable_keeps_a1_after_a0_is_overwritten(self):
        text = u32s(
            la_pcrel(0x0D, 1, 4),
            la_2ri12(0x00B, 0, 4, 5),
            la_2ri12(0x00B, 25, 4, 8),
            la_2ri12(0x00E, 1, 0, 4),
            la_bl(0x3000 - 0x1010),
        )
        ro = b"org.kde.plasma.kcm.users\0User\0"
        elf = port_elf(EM_LOONGARCH, text, ro, la_plt(0x3000, 0x3100), [QMLUNCREATABLE])
        self.assertEqual(qd.cpp_qml_modules(elf), {"org.kde.plasma.kcm.users"})

    def test_loongarch_vector_copy_skips_a_metaobject_name(self):
        note = b"org.kde.private.kcms.notifications\0"
        meta = b"NotificationSettings\0"
        image = bytearray(32)
        image[16:24] = (0x100).to_bytes(8, "little")
        text = u32s(
            la_pcrel(0x0C, 0xC00, 4),
            la_2ri12(0x0B0, 0, 4, 0),
            la_2ri12(0x0B0, 16, 4, 1),
            la_2ri12(0x0B1, 0x10, 3, 0),
            la_2ri12(0x0B1, 0x20, 3, 1),
            la_2ri12(0x00B, 0x10, 3, 5),
            (0x2A << 15) | 4,
            la_bl(0x3000 - 0x101C),
        )
        elf = port_elf(
            EM_LOONGARCH,
            text,
            note + meta,
            la_plt(0x3000, 0x3100),
            [QMLREGISTER],
            [(0x4000, 0x2000 + len(note)), (0x4008, 0x2000)],
            extra=[(".data.rel.ro", SHT_PROGBITS, 2, 0x4000, 0, bytes(image))],
        )
        self.assertEqual(qd.cpp_qml_modules(elf), {"org.kde.private.kcms.notifications"})

    def test_clang_fixtures_provide_registered_modules_only(self):
        expect = {
            "FingerprintModel",
            "QKeySequence",
            "org.kde.example",
            "org.kde.plasma.kcm.users",
            "org.kde.private.kcms.lookandfeel",
            "org.kde.private.kcms.notifications",
            "org.kde.userfeedback.kcm",
        }
        cases = (
            (
                EM_RISCV,
                _RV_FIXTURE,
                [
                    (0x3B68, 1), (0x3B70, 2), (0x3B78, 3), (0x3B80, 4),
                    (0x3B88, 5), (0x3B90, 6), (0x3B98, 7), (0x3BA0, 8),
                ],
                [
                    (0x29C8, 0x697), (0x29D8, 0x661), (0x2A40, 0x686),
                    (0x2A50, 0x686), (0x2A60, 0x6BF),
                ],
            ),
            (
                EM_LOONGARCH,
                _LA_FIXTURE,
                [
                    (0x30AB8, 1), (0x30AC0, 2), (0x30AC8, 3), (0x30AD0, 4),
                    (0x30AD8, 5), (0x30AE0, 6), (0x30AE8, 7), (0x30AF0, 8),
                ],
                [
                    (0x20918, 0x5F7), (0x20928, 0x5C1), (0x20990, 0x5E6),
                    (0x209A0, 0x5E6), (0x209B0, 0x61F),
                ],
            ),
        )
        symbols = [
            "memcpy", "qmlregister", "qmlProtectModule", "qmlRegisterModule",
            "qmlRegisterUncreatableMetaObject", "memset", "make_object", "touch",
        ]
        for machine, packed, relas, relatives in cases:
            secs = _unpack_fixture(packed)
            parts = [
                (name, SHT_PROGBITS, 6 if name in (".text", ".plt") else 2, addr, 0, blob)
                for name, (addr, blob) in secs.items()
            ]
            elf = build_elf(machine, parts, [(name, 0, 0) for name in symbols], relas, relatives)
            found = qd.cpp_qml_modules(elf)
            self.assertEqual(found, expect)
            self.assertNotIn("NotificationSettings", found)
            self.assertNotIn("User", found)


def ppc_words(endian, *words):
    order = "little" if endian == "little" else "big"
    return b"".join((word & 0xFFFFFFFF).to_bytes(4, order) for word in words)


def ppc_d(op, rt, ra, simm):
    return ((op & 0x3F) << 26) | ((rt & 31) << 21) | ((ra & 31) << 16) | (simm & 0xFFFF)


def ppc_ds(op, rt, ra, disp, xo=0):
    return (
        ((op & 0x3F) << 26)
        | ((rt & 31) << 21)
        | ((ra & 31) << 16)
        | (((disp >> 2) & 0x3FFF) << 2)
        | (xo & 3)
    )


def ppc_x(xo, rs, ra, rb):
    return (
        (31 << 26)
        | ((rs & 31) << 21)
        | ((ra & 31) << 16)
        | ((rb & 31) << 11)
        | ((xo & 0x3FF) << 1)
    )


def ppc_bl(pc, target):
    disp = target - pc
    return (18 << 26) | (((disp >> 2) & 0xFFFFFF) << 2) | 1


def ppc_ha_lo(delta):
    lo = ((delta + 0x8000) & 0xFFFF) - 0x8000
    ha = (delta - lo) >> 16
    return ha, lo


def ppc_prefix_pair(suffix_op, rd, ra, imm, rbit):
    imm34 = imm + (1 << 34) if imm < 0 else imm
    hi = (imm34 >> 16) & 0x3FFFF
    lo = imm34 & 0xFFFF
    prefix = (1 << 26) | ((rbit & 1) << 20) | hi
    suffix = ((suffix_op & 0x3F) << 26) | ((rd & 31) << 21) | ((ra & 31) << 16) | lo
    return prefix, suffix


def asm(endian, *ops):
    """Encode ops at 0x1000. A callable is invoked with its PC. A tuple is a prefix."""
    words = []
    pc = 0x1000
    for op in ops:
        produced = op(pc) if callable(op) else op
        if isinstance(produced, tuple):
            words.extend(produced)
            pc += 4 * len(produced)
        else:
            words.append(produced)
            pc += 4
    return ppc_words(endian, *words)


_PPC_MTCTR = 0x7D8903A6
_PPC_BCTR = 0x4E800420
# .got at 0x10000, so the TOC base the scanner uses is .got + 0x8000.
_PPC_TOC = 0x18000


def ppc_toc_stub(got):
    ha, lo = ppc_ha_lo(got - _PPC_TOC)
    return (
        ppc_ds(62, 2, 1, 24),
        ppc_d(15, 12, 2, ha),
        ppc_ds(58, 12, 12, lo),
        _PPC_MTCTR,
        _PPC_BCTR,
    )


def ppc_pcrel_stub(pc, got):
    return ppc_prefix_pair(57, 12, 0, got - pc, 1) + (_PPC_MTCTR, _PPC_BCTR)


def ppc_toc_plt(endian, count):
    blob = b""
    for i in range(count):
        raw = ppc_words(endian, *ppc_toc_stub(0x3100 + 8 * i))
        if i + 1 < count:
            raw += b"\0" * 12
        blob += raw
    return blob


def ppc_elf(text, rodata, plt, symbols, relatives=None, extra=(), endian="little", toc=True):
    """ppc64/ppc64le object. TOC stubs live in .plt so the call scan does not execute them."""
    parts = [(".text", SHT_PROGBITS, 6, 0x1000, 0, text)]
    if rodata:
        parts.append((".rodata", SHT_PROGBITS, 2, 0x2000, 0, rodata))
    parts.extend(extra)
    if toc:
        parts.append((".got", SHT_PROGBITS, 3, 0x10000, 0, b"\0" * 8))
    parts.append((".plt", SHT_PROGBITS, 6, 0x3000, 0, plt))
    relas = [(0x3100 + 8 * i, i + 1) for i in range(len(symbols))]
    return build_elf(
        EM_PPC64,
        parts,
        [(name, 0, 0) for name in symbols],
        relas,
        relatives,
        endian=endian,
        relative_type=22,
    )


def _unpack_sections(text, names):
    raw = zlib.decompress(base64.b64decode(text))
    out = {}
    pos = 0
    for name in names:
        addr, size = struct.unpack_from("<QI", raw, pos)
        pos += 12
        out[name] = (addr, raw[pos:pos + size])
        pos += size
    return out


_PPC64LE_FIXTURE = (
    "eNq9U89rE0EUfpMNEgQh9NSLdXvR6GHb40gqREGJaMNK6aG33W6mdc2PjclWImzcjagoKNTbnkqCEjzmT2io4jVX"
    "b/UWycFKDgYCxjfJJI2yFkHwwbLzfe/tfN97M1s7RYCHKgGE4PaK9uwabYQiTmdYHxwNWwMyXOwPh5srCajH30GN"
    "Rgnpq5WDeLn7xo2QPVoJQXIernYANFruvnWrAu8ilqFK/YUFJ4pr/1zY8fgbMa/1L4CzLmo/wyNKwKMbAs8JDKg3"
    "4ZKwh5yGTw35fVrufXTR9uXViT5ZpFEgHfTcRe/dhhRxZPBSMBO8P+/pr/19+zHur416Udy7jX3K2GMMWnG1YlD/"
    "vOTsY85fOuvw9SEYzwExn8UR6H65d+D6X8H5RMY+JD6H3ntXg/ogBvouEI3eBb3Je2aiJvoXXjE07rf9ZOyXe30t"
    "zuIe6ntA+okZjzf5rHGmOveK2P8OzobQm+gC4TPV6Bx6x/4Gy1Bt8jOSQe9fuph2KL5bMK7lffEz+CBwAY49n+RX"
    "Fn5b6FVFj9xvU8x24nV0L5bCTg05X5Gm9+G60EoIrSCdCBy6kwdr+wQeX5n3Xn5pSC8qcthLTTg5gIsFcMsBHA3g"
    "EgFcMoBTZzj1dGjkuYm/2HqJFeHOLfZwjd3fYXmDgVXcVjJpprCynitkGdww89usWCiaeXvVSrMspCzb3DIN3Tat"
    "/BqzbcyXpl8VsnoppysZI6fs4NbHCY62GEtv6kaGp+EPQeDk+C0/1S2aD3Sb8Z1LStayMno+jXLZ4IL8TAul0T4S"
    "/GMQ+D9xJiTuG8ar8fonsN1ckA=="
)
_PPC64LE_RELAS = [(0x30d18, 1), (0x30d20, 2), (0x30d28, 3), (0x30d30, 4), (0x30d38, 5), (0x30d40, 6), (0x30d48, 7), (0x30d50, 8)]
_PPC64LE_RELS = [(0x20ad8, 0x20a72), (0x20ae8, 0x20a72), (0x20af8, 0x20a55), (0x20b90, 0x20b31), (0x20ba0, 0x20a83)]

_PPC64_FIXTURE = (
    "eNq9Uz1oU1EYPTcRDIIgnTomW80QOj5pC3ZQItISqR2FvL7clkfSvPxVXiFpXuwSQSFjpvJKpWSMmw5CQgUduwiO"
    "dYtkEbI8CFjPTZpY9GkHwQ/Cud/57v2+c95HHl8XULEYBMqhwLEX+f7aEyK5uHF+PjwU2sIhGpqL1rAr7i6cVBLV"
    "o76tHYhQHIF8bxmzWhKovurbzHdU3ttDU6sh5+mo89wgOsQ60SY6xJx6w/v3Ru/38EVzIJg/uMhnVM6ZmHIHiPON"
    "4oTW4bwPA/sORvUlVe9TW59atYhI9ARulUPB41UHYVyK37wpP11qoTfNqCTq+zhr1oTW28dnrybmiafEOeLHnoEO"
    "z2HqgqeL2erJgH7Fpwv/wer7gd1Qvlijv5aWFGjraI99iicTjVfrO1/2BE6VPs7KU5s57LIP9dG3Xt4PR8tHc9Hy"
    "yXy0bmC3yVleDa53iKbXRYOz1tUsoj7S5ggoDe0aHHpVe5y52E2Ofe1y6nZU7SkOvFX3la/xLvBurJmzI+hcoTlP"
    "ze5IM3Xye72hpjqxTXSILX4/l2d7Mnv8XTDefwSNP/WvniE0+Xm8u/QM4usLx648V3evhS9xjg9X9+EaPlzTh3N9"
    "uJYP155w324ERprb/DutF2UBjx7K3TWZ35FZQ8IqbMXSKRmTtr6dy0jcN7NbspArmNnSipWSGaxaJXPTNPSSaWXX"
    "ZKnEenH6KpfRi9t6LG1sx3bY+mdBZZtSpjZ0I63K8A+Bv8ev9encgvlUL0nVuRjLWFZaz6Y4LuN/IXvJQnHSKoh/"
    "CIH/E+7N8e5CEyLw0v0B6bJlCw=="
)
_PPC64_RELAS = [(0x30e18, 1), (0x30e20, 2), (0x30e28, 3), (0x30e30, 4), (0x30e38, 5), (0x30e40, 6), (0x30e48, 7), (0x30e50, 8)]
_PPC64_RELS = [(0x20b78, 0x20b12), (0x20b88, 0x20b12), (0x20b98, 0x20af5), (0x20c30, 0x20bd1), (0x20c40, 0x20b23)]

_PPC64_ELFV2_FIXTURE = (
    "eNq9U01oE0EU/iYRCYJQeupxCx5iDiHHlVaIghJQy0opeFDIdjMtSzY/TbaSwqbZGJUULFTwkFNpqJQec/TYEMUe"
    "e+wxHoSVXIRcFnKIbzY/hrKIIPhg+ea9NzPf971hP1xnEPE8CCw/RkC+/9awQoGTQXPoDNrDn+7ikC2vD4dLTcTl"
    "I5y6jM0tdcqKfMhCleNeKYHAnnMPC3ISqHz08n2RO7toyFVIrgqZ1geEMcI6YZjQJpTEGdr/wju/i2+yDUa5Os7n"
    "RU6cmNYOkaAzosbkM+L72i/dgddf8/qLDA7DnBUKnvRIe488rNiQMBMjj2/qnscZf56vNmkij7JWVuo1dBtVOE4N"
    "l24VXcILwkvCc0fDGa0vSB/Ix3ml0yff7Pt4DsHK537pQPijHvWfyUmGlorkyC/L+Gn11/m6O6PzvdBJnFukUXcZ"
    "bKGT5qBaNSliHYcjVicWqWvYaRAn6VPcJhJuG3Hi9GZMqHvcNoPQ0qoiRp7Fu86P30oatBG2Urcj4t0SQFvsF/5G"
    "b4MvI+3I/4X2W0I76W6TVsXTTnppfp9Im0zYIowRntI8j2gdnmgYzQkPxlzxCddVnkoXocnn0t67r8B+vLMXynti"
    "7zVppib51MI+tZhPTfapxX1qCZ+aMqkpNwKe5hb9bmtFXsDTR3xnlW9t86zGkStsRtMpHuUlNZM3OB7q2U1eyBf0"
    "rPkkl+IGVnKmvqFrqqnnsqvcNKlfnJ7KG2oxo0bTWia6TVf/bohsg/PUuqqlRRv+wfDnuNqf8hb0l6rJxc3FqJHL"
    "pdVsiugM/w3ZGQvFyVVB/EMw/Ke4OXq70CQP7OMX8cNvEw=="
)
_PPC64_ELFV2_RELAS = [(0x30d18, 1), (0x30d20, 2), (0x30d28, 3), (0x30d30, 4), (0x30d38, 5), (0x30d40, 6), (0x30d48, 7), (0x30d50, 8)]
_PPC64_ELFV2_RELS = [(0x20ad8, 0x20a72), (0x20ae8, 0x20a72), (0x20af8, 0x20a55), (0x20b90, 0x20b31), (0x20ba0, 0x20a83)]

_PPC64LE_P10_FIXTURE = (
    "eNq9U89rE0EUfrMTSvAU/4PkUKgHQxGElV4iUkkRC1K9Z0ymdUmyG5ONrFDJxot6S2/FU4QaeszVW4IechEKIgS8"
    "VKRQWA8Rewgsun6ziTXEmgiCD5Z587734/vesJ0FRsoynKilRbcPgqt+PxD+YVD3j4MX/iDo+jFiw4/fE8M0dVcY"
    "xRb2Iq6+T03d+fq6dlGjNKrhv6kprMebYY7D3fB8zDPhuRZJrSSQCz/ymVMvTnVd1V0mdqJiPcQW4X9jlHbeXUMv"
    "Vz+PfOdDNvQJ80b1mOXtIdZErKM7n57UQP/KIrA4S+jgegzOXotHt8Hfgw4Pejzo8uLkrtOEKb0/9X2BPsWjAR6K"
    "7y7PPF2CXqKR1qMxT+ypNyCxq7S/R+wt+PKx/iUSjQMSeobEkFhGv0eirTReQE5sits0F/UEk3x2ghGf5xr1gEXW"
    "iPXVvY77KuZW0XMHHzE33GMKXJep3lZ7jWM+uPuq7hKJEx33l4RdgrPa5Sv4Jfo7PopLd8zltvb7uy1ro3dbRc/U"
    "uOdZ/aJ0WNOQH4+4Ry3+7BHO6Xmk8OgcfMBn4/05eGcOvj8Hb8zBnRm4e04LzzZ+uTsVWaZbN+TDDXm/Ks2sJKu8"
    "lcznZFI6olgqSLpumFuyXCobpn3TyskCrVu2sWlkhW1Y5oa0beCV06pSQVSKIpnPFpNVtP4FqNumlLm7IptXMP3B"
    "GM22Kfx0btl4IGypOleSBcvKCzOHcYWzE8wJCZWwD6d/NEb/3X4AQ50k4A=="
)
_PPC64LE_P10_RELAS = [(0x30d40, 1), (0x30d48, 2), (0x30d50, 3), (0x30d58, 4), (0x30d60, 5), (0x30d68, 6), (0x30d70, 7), (0x30d78, 8)]
_PPC64LE_P10_RELS = [(0x20b08, 0x20aa2), (0x20b18, 0x20aa2), (0x20b28, 0x20a85), (0x20bc0, 0x20b61), (0x20bd0, 0x20ab3)]

_PPC64_P10_FIXTURE = (
    "eNq9U0FrE0EU/mZ3KcFT/AfJoaAHQxGElV4iUkkRC1K975hM65JkNyYbWaGyGy/qLb0VTxVq6TFXbwl6yEUoiBDw"
    "UpFCYT1E2kNh0fVNsraxjY0g9MEy8+Z7733fN8O2pxhkGCqwklA2wxvRdsijXtiIdsLX0V7YifqHDMnD9M8vsx3k"
    "ppJguq9t6OvY8t7tuzkoV3QD8N7vuxKbXVe7g9VXh7mhPhusWW2eatMa7btQv+kNpGTfAcO1+KxL++kc2A/v5kdX"
    "98Go/qKX/zzYEx9kP/UwbyNwKWd6m3iff3WvY4BN62mW2iOtAWleSaibAekPyEdAfgLyteAjhZEY+D329z3W0ZR6"
    "XxjqGvm9RNyQXknbbowbaxx96Z3OPpHeD6RJlf6bnOo5tg85DN1gaHE8lB6p5jJpS45qO6nFOKUnWh3yKa80wnoM"
    "83HeIN45mlmnb1X3GSQHac22GpiR90r8qZC0y74DjquU6zngjdQ8vFe81dOo/IueWEsn5r53+t2UmeG7YY5mZuXM"
    "cfO8HSSoXtn1tdTTlxLX/sBlxHjibFztT8B7E/D2BHxrAt6cgLt/w/0LymBt0S93vyaquHtbPFkUj+rCygvY1eVM"
    "sSAywuXlSknglmkti2qlalrOHbsgSliwHXPJzHPHtK1F4TiE1466KiVeK/NMMV/O1Gn0MSCzJSEKD3i+KGGMD4az"
    "4yR+xFs1H3NHyMm1TMm2i9wqEF1pfIE1YqH2e5SK/wiG849foQUk4A=="
)
_PPC64_P10_RELAS = [(0x30d40, 1), (0x30d48, 2), (0x30d50, 3), (0x30d58, 4), (0x30d60, 5), (0x30d68, 6), (0x30d70, 7), (0x30d78, 8)]
_PPC64_P10_RELS = [(0x20b08, 0x20aa2), (0x20b18, 0x20aa2), (0x20b28, 0x20a85), (0x20bc0, 0x20b61), (0x20bd0, 0x20ab3)]


_PPC_SYMBOLS = [
    "memcpy", "qmlregister", "qmlProtectModule", "qmlRegisterModule",
    "qmlRegisterUncreatableMetaObject", "memset", "make_object", "touch",
]
_PPC_SECTIONS = (".text", ".data.rel.ro", ".got")


class PowerPCTests(unittest.TestCase):
    """Call scanner for big-endian ppc64 and little-endian ppc64le.

    Medium code addresses a URI with addis/addi from r2. r2 is .got+0x8000,
    which is also where the ELFv2 global-entry prologue puts it. Power10
    uses paddi/pld @pcrel and has no TOC. The Clang fixtures are embedded
    so the check does not need a PowerPC compiler.
    """

    def test_branch_and_prefix_immediates_roundtrip(self):
        # bl from the ppc64le Clang fixture, and a backward branch.
        self.assertEqual(qd._ppc_branch(0x4800027D, 0x106D4), (0x10950, 1))
        self.assertEqual(qd._ppc_branch(ppc_bl(0x1008, 0x3000), 0x1008), (0x3000, 1))
        self.assertEqual(qd._ppc_branch(ppc_bl(0x2000, 0x1000), 0x2000), (0x1000, 1))
        # paddi r4, 0, 66724, 1 at 0x106e4. PC + imm lands on a URI.
        self.assertEqual(qd._ppc_prefix_imm(0x06100001, 0x388004A4), 0x104A4)
        self.assertEqual(0x106E4 + 0x104A4, 0x20B88)

    def _protect(self, endian):
        ha, lo = ppc_ha_lo(0x2000 - _PPC_TOC)
        text = asm(
            endian,
            ppc_d(15, 3, 2, ha),
            ppc_d(14, 3, 3, lo),
            lambda pc: ppc_bl(pc, 0x3000),
        )
        elf = ppc_elf(
            text, b"org.kde.example\0", ppc_toc_plt(endian, 1), [QMLPROTECT], endian=endian
        )
        self.assertEqual(qd.cpp_qml_modules(elf), {"org.kde.example"})

    def test_little_endian_protect_module_from_the_toc(self):
        self._protect("little")

    def test_big_endian_protect_module_from_the_toc(self):
        self._protect("big")

    def _stack_uri(self, endian):
        ha, lo = ppc_ha_lo(0x2000 - _PPC_TOC)
        text = asm(
            endian,
            ppc_d(15, 30, 2, ha),
            ppc_d(14, 30, 30, lo),
            ppc_ds(58, 3, 30, 0),
            ppc_ds(58, 4, 30, 8),
            ppc_ds(62, 3, 1, 0x20),
            ppc_ds(62, 4, 1, 0x28),
            ppc_d(14, 5, 1, 0x20),
            ppc_ds(62, 5, 1, 0x40),
            ppc_d(14, 6, 0, 0x100),
            ppc_d(44, 6, 1, 0x48),
            ppc_d(14, 3, 0, 0),
            ppc_d(14, 4, 1, 0x40),
            lambda pc: ppc_bl(pc, 0x3000),
        )
        elf = ppc_elf(
            text, b"org.kde.example\0", ppc_toc_plt(endian, 1), [QMLREGISTER], endian=endian
        )
        self.assertEqual(qd.cpp_qml_modules(elf), {"org.kde.example"})

    def test_little_endian_stack_uri_is_captured_before_the_register_is_reused(self):
        self._stack_uri("little")

    def test_big_endian_stack_uri_is_captured_before_the_register_is_reused(self):
        self._stack_uri("big")

    def _uncreatable(self, endian):
        ha, lo = ppc_ha_lo(0x2000 - _PPC_TOC)
        text = asm(
            endian,
            ppc_d(15, 3, 2, ha),
            ppc_d(14, 4, 3, lo),
            ppc_d(14, 7, 3, lo + 25),
            ppc_d(14, 3, 0, 1),
            lambda pc: ppc_bl(pc, 0x3000),
        )
        ro = b"org.kde.plasma.kcm.users\0User\0"
        elf = ppc_elf(text, ro, ppc_toc_plt(endian, 1), [QMLUNCREATABLE], endian=endian)
        self.assertEqual(qd.cpp_qml_modules(elf), {"org.kde.plasma.kcm.users"})

    def test_little_endian_uncreatable_reads_the_uri_not_the_type_name(self):
        self._uncreatable("little")

    def test_big_endian_uncreatable_reads_the_uri_not_the_type_name(self):
        self._uncreatable("big")

    def _memcpy_image(self, endian):
        note = b"org.kde.private.kcms.notifications\0"
        meta = b"NotificationSettings\0"
        image = bytearray(0x60)
        # QTypeRevision is a uint16 at the low address, then six zero bytes.
        # Big-endian is 01 00 00 00 00 00 00 00, not a big-endian uint64.
        image[0x40:0x48] = (0x100).to_bytes(2, endian) + b"\0" * 6
        ha, lo = ppc_ha_lo(0x4000 - _PPC_TOC)
        text = asm(
            endian,
            ppc_d(15, 4, 2, ha),
            ppc_d(14, 4, 4, lo),
            ppc_d(14, 3, 1, 0x20),
            ppc_d(14, 5, 0, 0x60),
            lambda pc: ppc_bl(pc, 0x3000),
            ppc_d(14, 3, 0, 0),
            ppc_d(14, 4, 1, 0x20),
            lambda pc: ppc_bl(pc, 0x3020),
        )
        elf = ppc_elf(
            text,
            note + meta,
            ppc_toc_plt(endian, 2),
            ["memcpy", QMLREGISTER],
            [(0x4038, 0x2000), (0x4048, 0x2000 + len(note))],
            extra=[(".data.rel.ro", SHT_PROGBITS, 3, 0x4000, 0, bytes(image))],
            endian=endian,
        )
        found = qd.cpp_qml_modules(elf)
        self.assertEqual(found, {"org.kde.private.kcms.notifications"})
        self.assertNotIn("NotificationSettings", found)

    def test_little_endian_memcpy_image_skips_a_metaobject_name(self):
        self._memcpy_image("little")

    def test_big_endian_memcpy_image_skips_a_metaobject_name(self):
        self._memcpy_image("big")

    def test_little_endian_vector_copy_skips_a_metaobject_name(self):
        note = b"org.kde.private.kcms.notifications\0"
        meta = b"NotificationSettings\0"
        image = bytearray(32)
        image[16:24] = (0x100).to_bytes(8, "little")
        ha, lo = ppc_ha_lo(0x4000 - _PPC_TOC)
        text = asm(
            "little",
            ppc_d(15, 30, 2, ha),
            ppc_d(14, 30, 30, lo),
            ppc_d(14, 5, 0, 0),
            ppc_x(844, 0, 30, 5),
            ppc_d(14, 5, 0, 16),
            ppc_x(844, 1, 30, 5),
            ppc_d(14, 5, 0, 0),
            ppc_x(972, 0, 1, 5),
            ppc_d(14, 5, 0, 16),
            ppc_x(972, 1, 1, 5),
            ppc_d(14, 3, 0, 0),
            ppc_d(14, 4, 1, 0),
            lambda pc: ppc_bl(pc, 0x3000),
        )
        elf = ppc_elf(
            text,
            note + meta,
            ppc_toc_plt("little", 1),
            [QMLREGISTER],
            [(0x4000, 0x2000 + len(note)), (0x4008, 0x2000)],
            extra=[(".data.rel.ro", SHT_PROGBITS, 3, 0x4000, 0, bytes(image))],
        )
        found = qd.cpp_qml_modules(elf)
        self.assertEqual(found, {"org.kde.private.kcms.notifications"})
        self.assertNotIn("NotificationSettings", found)

    def _pcrel(self, endian):
        text = asm(
            endian,
            lambda pc: ppc_prefix_pair(14, 3, 0, 0x2000 - pc, 1),
            lambda pc: ppc_bl(pc, 0x3000),
        )
        plt = ppc_words(endian, *ppc_pcrel_stub(0x3000, 0x3100))
        elf = ppc_elf(
            text, b"org.kde.example\0", plt, [QMLPROTECT], endian=endian, toc=False
        )
        self.assertEqual(qd.cpp_qml_modules(elf), {"org.kde.example"})

    def test_little_endian_pcrel_protect_module(self):
        self._pcrel("little")

    def test_big_endian_pcrel_protect_module(self):
        self._pcrel("big")

    def test_clang_fixtures_provide_registered_modules_only(self):
        expect = {
            "FingerprintModel",
            "QKeySequence",
            "org.kde.example",
            "org.kde.plasma.kcm.users",
            "org.kde.private.kcms.lookandfeel",
            "org.kde.private.kcms.notifications",
            "org.kde.userfeedback.kcm",
        }
        cases = (
            ("little", _PPC64LE_FIXTURE, _PPC64LE_RELAS, _PPC64LE_RELS),
            ("big", _PPC64_FIXTURE, _PPC64_RELAS, _PPC64_RELS),
            ("big", _PPC64_ELFV2_FIXTURE, _PPC64_ELFV2_RELAS, _PPC64_ELFV2_RELS),
            ("little", _PPC64LE_P10_FIXTURE, _PPC64LE_P10_RELAS, _PPC64LE_P10_RELS),
            ("big", _PPC64_P10_FIXTURE, _PPC64_P10_RELAS, _PPC64_P10_RELS),
        )
        for endian, packed, relas, relatives in cases:
            secs = _unpack_sections(packed, _PPC_SECTIONS)
            parts = []
            for name, (addr, blob) in secs.items():
                if not addr and not blob:
                    continue
                flags = 6 if name == ".text" else 3
                parts.append((name, SHT_PROGBITS, flags, addr, 0, blob))
            elf = build_elf(
                EM_PPC64,
                parts,
                [(name, 0, 0) for name in _PPC_SYMBOLS],
                relas,
                relatives,
                endian=endian,
                relative_type=22,
            )
            found = qd.cpp_qml_modules(elf)
            self.assertEqual(found, expect)
            self.assertNotIn("NotificationSettings", found)
            self.assertNotIn("User", found)
            self.assertNotIn("KCMLookandFeel", found)

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
