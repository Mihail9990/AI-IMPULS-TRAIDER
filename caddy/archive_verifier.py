"""Strict ZIP validator algorithm from clean AI-IMPULS commit 4813051.
Pinned release deliberately stays RECOVERY 2.11.9 for launcher compatibility.
"""

import hashlib, io, stat, struct, zipfile

VERSION='2.11.9'

ARCHIVE_SHA256='ad93112d335ee687100c4c9f11bced16ca590377976b7fe7883882b9e797e472'

EXE_SHA256='2622e239c79df700b0eb92cbd950d1edee365e155a7859f358f0e55f002df591'

DOWNLOAD='https://github.com/caddyserver/caddy/releases/download/v2.11.9/caddy_2.11.9_windows_amd64.zip'

class HttpsError(Exception): pass

MAX_ARCHIVE=64*1024*1024

MAX_EXE=128*1024*1024

def validate_archive(data):
    """Return only the pinned executable bytes. Never extract archive paths."""
    if not isinstance(data,bytes) or len(data)>MAX_ARCHIVE or digest(data)!=ARCHIVE_SHA256:
        raise HttpsError('Нужен неизменённый официальный Caddy '+VERSION+' Windows amd64 ZIP: SHA256 не совпал. Другие версии не поддерживаются.')
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            entries=z.infolist();names=[i.filename for i in entries]
            if not entries or len(names)!=len(set(names)) or set(names)-{'LICENSE','README.md','caddy.exe'} or 'caddy.exe' not in names:
                raise ValueError()
            binary=None
            for i in entries:
                mode=(i.external_attr>>16)&0xffff
                if i.is_dir() or (mode and stat.S_IFMT(mode) not in (0,stat.S_IFREG)) or i.flag_bits&1 or i.compress_type not in (zipfile.ZIP_STORED,zipfile.ZIP_DEFLATED):raise ValueError()
                if i.file_size<0 or i.file_size>(MAX_EXE if i.filename=='caddy.exe' else 1024*1024):raise ValueError()
                value=z.read(i)  # CRC verification; size bounded before decompression.
                if i.filename=='caddy.exe':binary=value
            if binary is None or len(binary)<64 or binary[:2]!=b'MZ':raise ValueError()
            pe=struct.unpack_from('<I',binary,0x3c)[0]
            if pe<64 or pe+26>len(binary) or binary[pe:pe+4]!=b'PE\0\0':raise ValueError()
            machine=struct.unpack_from('<H',binary,pe+4)[0]
            optional=struct.unpack_from('<H',binary,pe+20)[0]
            flags=struct.unpack_from('<H',binary,pe+22)[0]
            if machine!=0x8664 or optional<2 or pe+24+optional>len(binary) or flags&0x2000 or struct.unpack_from('<H',binary,pe+24)[0]!=0x20b:raise ValueError()
            return binary
    except (ValueError,KeyError,struct.error,zipfile.BadZipFile,RuntimeError,NotImplementedError):
        raise HttpsError('ZIP повреждён или содержит неподдерживаемые файлы/архитектуру. Нужен официальный архив Windows amd64; ничего не извлечено.') from None

def digest(data): return hashlib.sha256(data).hexdigest()
