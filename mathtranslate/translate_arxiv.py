from . import utils
from . import process_latex
from . import process_file
from .pdf_compiler import compile_tex_to_pdf
from .translate import translate_single_tex_file
from .encoding import get_file_encoding
from . import app_dir
import os
import sys
import stat
import types
import shutil
import gzip
import zipfile
import tarfile
import tempfile
import urllib.request


def download_source(number, path):
    url = f'https://arxiv.org/e-print/{number}'
    print('trying to download from', url)
    urllib.request.urlretrieve(url, path)


def download_source_with_cache(number, path):
    cache_dir = os.path.join(app_dir, 'cache_arxiv')
    os.makedirs(cache_dir, exist_ok=True)
    cache_path = os.path.join(cache_dir, 'last_downloaded_source')
    cache_number_path = os.path.join(cache_dir, 'last_arxiv_number')
    if os.path.exists(cache_path) and os.path.exists(cache_number_path):
        with open(cache_number_path, encoding='utf-8') as f:
            last_number = f.read()
        if last_number == number:
            shutil.copyfile(cache_path, path)
            return
    download_source(number, path)
    shutil.copyfile(path, cache_path)
    with open(cache_number_path, 'w', encoding='utf-8') as f:
        f.write(number)


def is_pdf(filename):
    with open(filename, 'rb') as f:
        return f.readline()[0:4] == b'%PDF'


def loop_files(dir):
    all_files = []
    for root, dirs, files in os.walk(dir):
        for file in files:
            all_files.append(os.path.join(root, file))
    return all_files


def _validate_archive_member_path(member_name, dest_real):
    if not member_name or "\x00" in member_name:
        raise ValueError(f"Unsafe archive entry path: {member_name!r}")
    normalized = member_name.replace("\\", "/")
    if normalized.startswith("/") or (len(normalized) >= 2 and normalized[1] == ":"):
        raise ValueError(f"Unsafe absolute archive entry path: {member_name!r}")
    parts = [p for p in normalized.split("/") if p not in ("", ".")]
    if ".." in parts:
        raise ValueError(f"Unsafe archive path traversal blocked: {member_name!r}")
    target_path = os.path.realpath(os.path.join(dest_real, *parts)) if parts else dest_real
    if os.path.commonpath([dest_real, target_path]) != dest_real:
        raise ValueError(f"Archive entry escapes destination directory: {member_name!r}")
    return target_path


def safe_extract_zip(zip_path, dest_dir):
    dest_real = os.path.realpath(dest_dir)
    with zipfile.ZipFile(zip_path, 'r') as zf:
        validated = []
        for member in zf.infolist():
            mode = member.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise ValueError(f"Unsafe symlink in zip archive: {member.filename!r}")
            target_path = _validate_archive_member_path(member.filename, dest_real)
            validated.append((member, target_path))
        for member, target_path in validated:
            if member.is_dir():
                os.makedirs(target_path, exist_ok=True)
            else:
                parent = os.path.dirname(target_path)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                with zf.open(member, 'r') as src, open(target_path, 'wb') as dst:
                    shutil.copyfileobj(src, dst)


def safe_extract_tar(tar_path, dest_dir):
    dest_real = os.path.realpath(dest_dir)
    with tarfile.open(tar_path, mode='r') as tf:
        for member in tf.getmembers():
            if member.issym() or member.islnk():
                raise ValueError(f"Unsafe link in tar archive: {member.name!r}")
            _validate_archive_member_path(member.name, dest_real)
        if hasattr(tarfile, 'data_filter'):
            tf.extractall(dest_real, filter='data')
        else:
            tf.extractall(dest_real)


def zipdir(dir, output_path):
    # ziph is zipfile handle
    tmp_path = f'{output_path}.tmp'
    try:
        with zipfile.ZipFile(tmp_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for file in loop_files(dir):
                rel_path = os.path.relpath(file, dir)
                zipf.write(file, arcname=rel_path)
        os.replace(tmp_path, output_path)
    except BaseException:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def translate_dir(dir, options):
    files = loop_files(dir)
    texs = [f[0:-4] for f in files if f[-4:] == '.tex']
    bibs = [f[0:-4] for f in files if f[-4:] == '.bib']
    bbls = [f[0:-4] for f in files if f[-4:] == '.bbl']
    no_bib = len(bibs) == 0
    print('main tex files found:')
    complete_texs = []
    for tex in texs:
        path = f'{tex}.tex'
        input_encoding = get_file_encoding(path)
        with open(path, encoding=input_encoding) as f:
            content = f.read()
        content = process_latex.remove_tex_comments(content)
        complete = process_latex.is_complete(content)
        if complete:
            print(path)
            process_file.merge_complete(tex)
            if no_bib and (tex in bbls):
                process_file.add_bbl(tex)
            complete_texs.append(tex)
    if len(complete_texs) == 0:
        return False
    for basename in texs:
        if basename in complete_texs:
            continue
        os.remove(f'{basename}.tex')
    for basename in bbls:
        os.remove(f'{basename}.bbl')
    if options.notranslate:
        return True
    for filename in complete_texs:
        print(f'Processing {filename}')
        file_path = f'{filename}.tex'
        translate_single_tex_file(file_path, file_path, options.engine, options.l_from, options.l_to, options.debug, options.nocache, options.threads)
    return True


def translate_zip_project(zip_path, output_zip_path, engine, l_from, l_to, debug=False, nocache=False, threads=0):
    options = types.SimpleNamespace(
        engine=engine,
        l_from=l_from,
        l_to=l_to,
        debug=debug,
        nocache=nocache,
        threads=threads,
        notranslate=False,
    )
    with tempfile.TemporaryDirectory() as temp_dir:
        safe_extract_zip(zip_path, temp_dir)
        success = translate_dir(temp_dir, options)
        if not success:
            return False
        out_dir = os.path.dirname(os.path.abspath(output_zip_path))
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        zipdir(temp_dir, output_zip_path)
    return True


def main(args=None, require_updated=False):
    '''
    There are four types of a downdload arxiv project
    1. It is simply a PDF file (cannot translate)
    2. It is a gzipped text file, but the text file contains nothing meaningful (cannot translate)
    3. It is a gzipped tex file (can translate)
    4. It is a gzipped + tarzipped tex project (can translate)

    return False for the first two cases
    return True if the translation is successful (last two cases)

    to call this function from python,
    you can do e.g `main(['2205.15510', '-o', 'output.zip'])`
    '''
    utils.check_update(require_updated=require_updated)

    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("number", nargs='?', type=str, help='arxiv number')
    parser.add_argument("-o", type=str, help='output path')
    parser.add_argument("--from_dir", action='store_true')
    parser.add_argument("--notranslate", action='store_true')  # debug option
    parser.add_argument("--compile", action='store_true', help='compile translated main .tex to PDF using XeLaTeX')
    utils.add_arguments(parser)
    options = parser.parse_args(args)
    utils.process_options(options)

    if options.number is None:
        parser.print_help()
        sys.exit()

    number = options.number
    print('arxiv number:', number)
    print()
    safe_number = number.replace('/', '-')
    if options.o is None:
        output_path = f'{safe_number}.zip'
    else:
        output_path = options.o

    success = True
    with tempfile.TemporaryDirectory() as temp_dir:
        print('temporary directory', temp_dir)
        if options.from_dir:
            shutil.copytree(number, temp_dir, dirs_exist_ok=True)
            success = translate_dir(temp_dir, options)
        else:
            download_path = os.path.join(temp_dir, safe_number)
            try:
                download_source_with_cache(number, download_path)
            except BaseException:
                print('Cannot download source, maybe network issue or wrong link')
                return False
            if is_pdf(download_path):
                # case 1
                success = False
            else:
                with open(download_path, "rb") as f:
                    content = gzip.decompress(f.read())
                with open(download_path, "wb") as f:
                    f.write(content)
                try:
                    # case 4
                    safe_extract_tar(download_path, temp_dir)
                    os.remove(download_path)
                except tarfile.ReadError:
                    # case 2 or 3
                    print('This is a pure text file')
                    shutil.move(download_path, os.path.join(temp_dir, 'main.tex'))
                success = translate_dir(temp_dir, options)
        if success:
            # case 3 or 4
            out_dir = os.path.dirname(os.path.abspath(output_path))
            if out_dir:
                os.makedirs(out_dir, exist_ok=True)
            zipdir(temp_dir, output_path)
            if getattr(options, 'compile', False):
                tex_candidates = [f for f in loop_files(temp_dir) if f.endswith('.tex')]
                main_tex = next((f for f in tex_candidates if os.path.basename(f) == 'main.tex'), tex_candidates[0] if tex_candidates else None)
                if main_tex:
                    comp_res = compile_tex_to_pdf(main_tex, work_dir=os.path.dirname(main_tex))
                    print(comp_res.message)
                    if comp_res.status == 'success' and comp_res.pdf_path:
                        pdf_out = os.path.splitext(output_path)[0] + '.pdf'
                        shutil.copyfile(comp_res.pdf_path, pdf_out)
                        print('PDF saved to', pdf_out)

    if success:
        print('zip file is saved to', output_path)
        print('You can upload the zip file to overleaf to autocompile')
        return True
    else:
        print('Source code is not available for arxiv', number)
        return False
