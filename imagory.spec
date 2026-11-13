# PyInstaller spec for Imagory.  Build with:  uv run pyinstaller imagory.spec
# Produces a single-file build (dist/Imagory.exe on Windows, dist/Imagory on Linux). The same spec will later
# produce the zipped folder build (PRD §13) by switching ONEFILE to False.

ONEFILE = True

# Qt modules Imagory never uses. QtNetwork is excluded on purpose: the app is offline.
QT_EXCLUDES = [
    f"PySide6.{name}"
    for name in (
        "QtNetwork", "QtQml", "QtQuick", "QtQuickWidgets", "QtQuickControls2", "QtSql",
        "QtTest", "QtXml", "QtSvg", "QtSvgWidgets", "QtPdf", "QtPdfWidgets", "QtDBus",
        "QtOpenGL", "QtOpenGLWidgets", "QtConcurrent", "QtPrintSupport", "QtDesigner",
        "QtHelp", "QtUiTools", "QtMultimedia", "QtMultimediaWidgets", "QtNetworkAuth",
        "QtWebSockets", "QtBluetooth", "QtNfc", "QtPositioning", "QtSerialPort",
        "QtStateMachine", "QtCharts", "QtDataVisualization", "Qt3DCore",
    )
]

a = Analysis(
    ["imagory/__main__.py"],
    pathex=["."],
    binaries=[],
    datas=[("LICENSE", "."), ("THIRD_PARTY_LICENSES.txt", "."), ("assets", "assets")],
    hiddenimports=["pi_heif"],
    # pillow_heif is a dev-only dependency (it writes HEIC test fixtures); it bundles the GPLv2
    # x265 encoder and must never ship. The app decodes HEIC with pi_heif.
    excludes=["tkinter", "unittest", "pydoc", "pytest", "pillow_heif", *QT_EXCLUDES],
    noarchive=False,
)
pyz = PYZ(a.pure)

if ONEFILE:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name="Imagory",
        console=False,
        icon="assets/imagory.ico",
        upx=False,
        strip=False,
    )
else:
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Imagory", console=False,
              icon="assets/imagory.ico")
    coll = COLLECT(exe, a.binaries, a.datas, name="Imagory", upx=False, strip=False)
