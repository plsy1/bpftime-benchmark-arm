import os
import sys

def patch_file(filepath, replacements):
    if not os.path.exists(filepath):
        print(f"Warning: File {filepath} not found, skipping patch.")
        return
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    
    modified = False
    for target, replacement in replacements:
        if target in content:
            content = content.replace(target, replacement)
            modified = True
            
    if modified:
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(content)
        print(f"Successfully patched {filepath}")
    else:
        print(f"No changes needed for {filepath} (already patched or target not found)")

def main():
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    
    # Path to bpftool libbpf files
    libbpf_c_path = os.path.join(base_dir, "third_party", "bpftool", "libbpf", "src", "libbpf.c")
    bpf_helpers_h_path = os.path.join(base_dir, "third_party", "bpftool", "libbpf", "src", "bpf_helpers.h")
    
    libbpf_c_replacements = [
        # 1. kallsyms_cb
        (
            'struct bpf_object *obj = ctx;\n\tconst struct btf_type *t;\n\tstruct extern_desc *ext;\n\tchar *res;\n\n\tres = strstr(sym_name, ".llvm.");',
            'struct bpf_object *obj = ctx;\n\tconst struct btf_type *t;\n\tstruct extern_desc *ext;\n\tconst char *res;\n\n\tres = strstr(sym_name, ".llvm.");'
        ),
        # 2. avail_kallsyms_cb
        (
            'char sym_trim[256], *psym_trim = sym_trim, *sym_sfx;',
            'char sym_trim[256], *psym_trim = sym_trim;\n\t\tconst char *sym_sfx;'
        ),
        # 3. resolve_full_path
        (
            'for (s = search_paths[i]; s != NULL; s = strchr(s, \':\')) {\n\t\t\tchar *next_path;\n\t\t\tint seg_len;\n\n\t\t\tif (s[0] == \':\')\n\t\t\t\ts++;\n\t\t\tnext_path = strchr(s, \':\');',
            'for (s = search_paths[i]; s != NULL; s = strchr(s, \':\')) {\n\t\t\tconst char *next_path;\n\t\t\tint seg_len;\n\n\t\t\tif (s[0] == \':\')\n\t\t\t\ts++;\n\t\t\tnext_path = strchr(s, \':\');'
        )
    ]
    
    bpf_helpers_h_replacements = [
        # 4. bpf_stream_vprintk conflict - wrap entire block in #if 0
        (
            'extern int bpf_stream_vprintk(int stream_id, const char *fmt__str, const void *args,\n\t\t\t      __u32 len__sz, void *aux__prog) __weak __ksym;',
            '#if 0\nextern int bpf_stream_vprintk(int stream_id, const char *fmt__str, const void *args,\n\t\t\t      __u32 len__sz, void *aux__prog) __weak __ksym;'
        ),
        (
            'bpf_stream_vprintk(stream_id, ___fmt, ___param, sizeof(___param), NULL);\\\n})',
            'bpf_stream_vprintk(stream_id, ___fmt, ___param, sizeof(___param), NULL);\\\n})\n#endif'
        ),
    ]
    
    patch_file(libbpf_c_path, libbpf_c_replacements)
    patch_file(bpf_helpers_h_path, bpf_helpers_h_replacements)

if __name__ == "__main__":
    main()
