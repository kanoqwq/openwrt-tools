//! Portable package discovery. No CWD search, downloads or device access.
use std::path::{Path, PathBuf};

pub fn app_bundle(path: &Path) -> Option<&Path> {
    path.ancestors().find(|p| p.extension().and_then(|ext| ext.to_str()).is_some_and(|ext| ext.eq_ignore_ascii_case("app")))
}

fn has_entry(root: &Path) -> bool {
    (root.join("OpenWrt-Installer.py").is_file()
        && (root.join("desktop/manifest.json").is_file()
            || root.join("files/device-profile.json").is_file()))
        || root.join("OpenWrt-Diff-Studio.py").is_file()
        || root.join("update.ota").is_file()
}

pub fn check_root(root: &Path, backend: &str) -> Result<PathBuf, String> {
    if !has_entry(root) {
        return Err(format!("No extracted OpenWrt package here / 此目录没有完整解压的 OpenWrt 安装包：\n{}\nSelect the folder containing the app, tools and package files.\n请选择同时包含 APP、tools 和安装文件的目录。", root.display()));
    }
    let runtime = root.join(backend);
    if !runtime.is_file() {
        return Err(format!("Package backend is missing / 安装包缺少后端：\n{}\nExtract the entire archive again; copying only the APP is insufficient.\n请重新完整解压安装包，不能只复制 APP。", runtime.display()));
    }
    root.canonicalize().map_err(|e| format!("Cannot access package / 无法访问安装包：\n{}\n{e}", root.display()))
}

pub fn selected_root(selected: &Path, backend: &str) -> Result<PathBuf, String> {
    if has_entry(selected) {
        return check_root(selected, backend);
    }
    // A user may select the APP, its Contents directory, or the payload folder.
    let root = if let Some(app) = app_bundle(selected) {
        app.parent().unwrap_or(selected)
    } else if selected.file_name().is_some_and(|n| n == "files" || n == "desktop") {
        selected.parent().unwrap_or(selected)
    } else {
        selected
    };
    // A damaged package must not silently switch to a different nested package.
    if has_entry(root) {
        return check_root(root, backend);
    }
    // Permit one archive wrapper only, and never choose between releases.
    let mut candidates = Vec::new();
    if let Ok(entries) = std::fs::read_dir(root) {
        for entry in entries.flatten() {
            let path = entry.path();
            if path.is_dir() && has_entry(&path) {
                candidates.push(path);
            }
        }
    }
    match candidates.len() {
        1 => check_root(&candidates[0], backend),
        0 => check_root(root, backend),
        _ => Err("Multiple OpenWrt packages found. Select the exact release folder.\n此目录包含多个安装包，请明确选择其中一个安装包目录。".into()),
    }
}

pub fn choose_root(
    backend: &str,
    mut pick: impl FnMut() -> Option<PathBuf>,
    mut report: impl FnMut(&str),
) -> Option<PathBuf> {
    while let Some(selected) = pick() {
        match selected_root(&selected, backend) {
            Ok(root) => return Some(root),
            Err(message) => report(&message),
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};
    const BACKEND: &str = "tools/macos/backend/openwrt-backend";
    static NEXT: AtomicUsize = AtomicUsize::new(0);
    struct Fixture(PathBuf);
    impl Fixture {
        fn new() -> Self {
            let root = std::env::temp_dir().join(format!("openwrt-path-{}-{}", std::process::id(), NEXT.fetch_add(1, Ordering::Relaxed)));
            std::fs::create_dir_all(&root).unwrap();
            Self(root)
        }
        fn file(root: &Path, name: &str) {
            let path = root.join(name);
            std::fs::create_dir_all(path.parent().unwrap()).unwrap();
            std::fs::write(path, b"fixture").unwrap();
        }
        fn package(&self, name: &str, marker: &str) -> PathBuf {
            let root = self.0.join(name);
            for file in ["OpenWrt-Installer.py", marker, BACKEND, "OpenWrt-Installer.app/Contents/MacOS/openwrt-installer"] {
                Self::file(&root, file);
            }
            root
        }
    }
    impl Drop for Fixture {
        fn drop(&mut self) { let _ = std::fs::remove_dir_all(&self.0); }
    }

    #[test]
    fn e5_and_mu300_support_spaces_unicode_and_app_selection() {
        let f = Fixture::new();
        for marker in ["files/device-profile.json", "desktop/manifest.json"] {
            let root = f.package(&format!("下载 固件 {}", marker.split('/').next().unwrap()), marker);
            for selection in [root.clone(), root.join("OpenWrt-Installer.app"), root.join("OpenWrt-Installer.app/Contents/MacOS"), root.join(marker).parent().unwrap().to_path_buf()] {
                assert_eq!(selected_root(&selection, BACKEND).unwrap(), root.canonicalize().unwrap());
            }
        }
    }
    #[test]
    fn explicit_wrapper_selection_requires_one_package() {
        let f = Fixture::new();
        let root = f.package("release", "desktop/manifest.json");
        assert_eq!(selected_root(&f.0, BACKEND).unwrap(), root.canonicalize().unwrap());
        f.package("other", "files/device-profile.json");
        assert!(selected_root(&f.0, BACKEND).unwrap_err().contains("Multiple"));
    }
    #[test]
    fn incomplete_package_reports_the_actual_missing_backend() {
        let f = Fixture::new();
        let root = f.package("release", "desktop/manifest.json");
        std::fs::remove_file(root.join(BACKEND)).unwrap();
        f.package("release/nested", "desktop/manifest.json");
        let message = selected_root(&root, BACKEND).unwrap_err();
        assert!(message.contains(BACKEND));
        assert!(message.contains("missing"));
    }
    #[test]
    fn translocated_or_copied_app_never_searches_elsewhere() {
        let f = Fixture::new();
        let app = f.0.join("AppTranslocation/random/d/OpenWrt-Installer.app");
        Fixture::file(&app, "Contents/MacOS/openwrt-installer");
        let root = app.parent().unwrap();
        assert!(check_root(root, BACKEND).is_err());
        let original = f.package("Downloads/release", "desktop/manifest.json");
        assert_eq!(selected_root(&original, BACKEND).unwrap(), original.canonicalize().unwrap());
    }
    #[test]
    fn empty_and_unextracted_paths_fail() {
        let f = Fixture::new();
        assert!(selected_root(&f.0, BACKEND).is_err());
        Fixture::file(&f.0, "release.zip");
        assert!(selected_root(&f.0.join("release.zip"), BACKEND).is_err());
        assert!(selected_root(&f.0.join("nonexistent"), BACKEND).is_err());
    }
    #[test]
    fn studio_and_exported_updater_keep_their_layouts() {
        let f = Fixture::new();
        for marker in ["OpenWrt-Diff-Studio.py", "update.ota"] {
            let root = f.0.join(marker.replace('.', "-"));
            Fixture::file(&root, marker);
            Fixture::file(&root, BACKEND);
            assert_eq!(check_root(&root, BACKEND).unwrap(), root.canonicalize().unwrap());
        }
    }
    #[test]
    fn incorrect_selection_can_retry_and_cancel_is_not_an_error() {
        let f = Fixture::new();
        let root = f.package("release", "desktop/manifest.json");
        let mut selections = [f.0.join("missing"), root.clone()].into_iter();
        let mut errors = Vec::new();
        let chosen = choose_root(BACKEND, || selections.next(), |e| errors.push(e.to_owned()));
        assert_eq!(chosen.unwrap(), root.canonicalize().unwrap());
        assert_eq!(errors.len(), 1);
        assert!(choose_root(BACKEND, || None, |_| panic!("Cancel must not report an error")).is_none());
        let mut selections = [f.0.join("missing")].into_iter();
        let mut count = 0;
        assert!(choose_root(BACKEND, || selections.next(), |_| count += 1).is_none());
        assert_eq!(count, 1);
    }
    #[cfg(unix)]
    #[test]
    fn macos_directory_aliases_resolve_without_losing_unicode() {
        let f = Fixture::new();
        let root = f.package("下载 固件", "desktop/manifest.json");
        let alias = f.0.join("alias");
        std::os::unix::fs::symlink(&root, &alias).unwrap();
        assert_eq!(selected_root(&alias, BACKEND).unwrap(), root.canonicalize().unwrap());
    }
}
