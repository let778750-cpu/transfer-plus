"""BabelDOC 0.7.1 adapter: capture the exact clean IR supplied to its writer.

The wheel does not enable Rust's optional stage-capture feature. Its public
MDocument.artifact() still exposes the final IR after run_part(). This wrapper
does not alter engine geometry, glyphs, or rendering operations.
"""
FORMULA_FONT_PATTERN = r"(CM[^RB]|(MS|XY|MT|BL|RM|EU|LA|RS)[A-Z]|LINE|LCIRCLE|TeX-|rsfs|txsy|wasy|stmary|.*Mono|.*Code|.*Sym|.*Math|AdvP4C4E74|AdvPSSym|AdvP4C4E59)"

def install_clean_capture():
    import base64
    import re
    from babeldoc.format.pdf.document_il.utils import formular_helper
    original_font_matcher = formular_helper.is_formulas_font
    def protected_font(font_name, pattern):
        name = font_name
        if name.startswith('BASE64:'):
            name = base64.b64decode(name[7:]).decode('latin1')
        # Scientific variable bases often use ordinary Times/Nimbus italic,
        # excluded by upstream's formula-font list. Keep these glyphs as objects,
        # including any italic words in the same run, rather than rewrite them.
        if re.search(r'ital|oblique', name, re.I):
            return True
        return original_font_matcher(font_name, pattern)
    formular_helper.is_formulas_font = protected_font
    from babeldoc import _rust_il
    from babeldoc.rust_debug_service import RustDebugSerializerService
    original = _rust_il.parse_prepared_pdf_product
    class AuditedDocument:
        def __init__(self, native):
            self.native = native
        def __getattr__(self, name):
            return getattr(self.native, name)
        def run_part(self, *args, **kwargs):
            result = self.native.run_part(*args, **kwargs)
            config = args[1]
            if not config.debug:
                RustDebugSerializerService().write_debug_json(
                    self.native.artifact(), str(config.get_working_file_path("final-layout.json")))
            return result
    def parse(*args, **kwargs):
        native = original(*args, **kwargs)
        return None if native is None else AuditedDocument(native)
    _rust_il.parse_prepared_pdf_product = parse
