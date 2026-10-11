"""Convert a binary Photoshop .asl layer style into the XML that Krita's
Node.setLayerStyleFromAsl() actually parses.

    python asl_to_kritaxml.py style.asl [out.aslxml]

Krita's format is NOT the standard ASL layout: it wants <asl> (no version)
holding one Descriptor classId="null" with the Lefx CONTENT directly inside
(Scl/masterFXSwitch/effect descriptors - no Lefx wrapper, no Nm/Idnt), plus a
Descriptor classId="Styl" holding only documentMode. Discovered by reading
layerStyleToAsl() back off a live layer; feeding it standard ASL XML "applies"
an empty style with no error (setLayerStyleFromAsl returns None here).

Made for actual_default_text.asl -> text_style.aslxml (2026-07-17, verified
in-Krita via the Save All bridge style_test). Bails out on patterns/rawdata -
capture those with the docker's "remember this layer's style" button instead.
"""

import struct
import sys
import xml.etree.ElementTree as ET


def _unicode(d, p):
    (n,) = struct.unpack_from(">I", d, p)
    p += 4
    return d[p : p + 2 * n].decode("utf-16-be").rstrip("\x00"), p + 2 * n


def _varkey(d, p):
    (n,) = struct.unpack_from(">I", d, p)
    p += 4
    n = n or 4
    return d[p : p + n].decode("latin1"), p + n


def _num(v):
    return str(int(v)) if v == int(v) else repr(v)


def _desc(d, p, parent, key):
    name, p = _unicode(d, p)
    classid, p = _varkey(d, p)
    el = ET.SubElement(parent, "node", {"type": "Descriptor", "name": name})
    if key is not None:
        el.set("key", key)
    el.set("classId", classid)
    (cnt,) = struct.unpack_from(">I", d, p)
    p += 4
    for _ in range(cnt):
        k, p = _varkey(d, p)
        typ = d[p : p + 4].decode("latin1")
        p += 4
        p = _item(d, p, el, k, typ)
    return el, p


def _item(d, p, parent, key, typ):
    def leaf(t, **attrs):
        e = ET.SubElement(parent, "node", {"type": t})
        if key is not None:
            e.set("key", key)
        for a, v in attrs.items():
            e.set(a, v)
        return e

    if typ == "Objc":
        _el, p = _desc(d, p, parent, key)
        return p
    if typ == "VlLs":
        (n,) = struct.unpack_from(">I", d, p)
        p += 4
        lst = leaf("List")
        for _ in range(n):
            t2 = d[p : p + 4].decode("latin1")
            p += 4
            p = _item(d, p, lst, None, t2)
        return p
    if typ == "doub":
        (v,) = struct.unpack_from(">d", d, p)
        leaf("Double", value=_num(v))
        return p + 8
    if typ == "UntF":
        u = d[p : p + 4].decode("latin1")
        (v,) = struct.unpack_from(">d", d, p + 4)
        leaf("UnitFloat", unit=u, value=_num(v))
        return p + 12
    if typ == "bool":
        leaf("Boolean", value=str(d[p]))
        return p + 1
    if typ == "long":
        (v,) = struct.unpack_from(">i", d, p)
        leaf("Integer", value=str(v))
        return p + 4
    if typ == "enum":
        tid, p = _varkey(d, p)
        val, p = _varkey(d, p)
        leaf("Enum", typeId=tid, value=val)
        return p
    if typ == "TEXT":
        s, p = _unicode(d, p)
        leaf("Text", value=s)
        return p
    raise SystemExit(f"unsupported OSType {typ!r} - use the docker's capture button instead")


def convert(src, dst):
    with open(src, "rb") as f:
        data = f.read()
    pos = 0
    ver, sig = struct.unpack_from(">H4s", data, pos)
    pos += 6
    if sig != b"8BSL" or ver != 2:
        raise SystemExit("not an 8BSL v2 .asl file")
    _patver, patlen = struct.unpack_from(">HI", data, pos)
    pos += 6
    if patlen:
        raise SystemExit("style has embedded patterns - use the capture button instead")
    (nstyles,) = struct.unpack_from(">I", data, pos)
    pos += 4
    if nstyles != 1:
        raise SystemExit(f"file holds {nstyles} styles, expected exactly 1")

    scratch = ET.Element("scratch")
    (slen,) = struct.unpack_from(">I", data, pos)
    pos += 4
    end = pos + slen
    pos += 4  # descriptor version (16)
    _ident, pos = _desc(data, pos, scratch, None)  # {Nm, Idnt} - dropped
    pos += 4
    styl, pos = _desc(data, pos, scratch, None)  # Styl {documentMode, Lefx}
    if pos != end:
        raise SystemExit("style block not fully consumed - refusing to guess")

    lefx = styl.find('node[@key="Lefx"]')
    if lefx is None:
        raise SystemExit("no Lefx (effects) block in this style")
    root = ET.Element("asl")
    null_el = ET.SubElement(root, "node", {"type": "Descriptor", "name": "", "classId": "null"})
    for child in list(lefx):
        null_el.append(child)
    styl_el = ET.SubElement(root, "node", {"type": "Descriptor", "name": "", "classId": "Styl"})
    ET.SubElement(
        styl_el,
        "node",
        {"type": "Descriptor", "name": "", "key": "documentMode", "classId": "documentMode"},
    )
    xml_text = ET.tostring(root, encoding="unicode")
    with open(dst, "w", encoding="utf-8", newline="\n") as f:
        f.write(xml_text)
    print(f"wrote {dst} - effects: {[n.get('key') for n in null_el.findall('node')]}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    src = sys.argv[1]
    dst = sys.argv[2] if len(sys.argv) > 2 else src.rsplit(".", 1)[0] + ".aslxml"
    convert(src, dst)
