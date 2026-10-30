#!/usr/bin/env bash
# =============================================================================
# ETF Quant · 直接调 aapt2 / javac / d8 / zipalign / apksigner 打 APK
#
# 为什么不用 Gradle：这套工程只有一个 Activity + 静态资源，引入 AGP/Gradle 需要
# 额外下载上百 MB 依赖，且版本矩阵容易踩坑。手工链路约 10 行命令，构建 10 秒内完成。
#
# 依赖：JDK 17、Android SDK（build-tools 34 + platforms/android-34）
# 用法：bash android/build.sh
# =============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

SDK="${ANDROID_SDK_ROOT:-C:/Users/macro/android-build/sdk}"
JDK="${JAVA_HOME:-C:/Users/macro/android-build/jdk/jdk-17.0.13+11}"
BT="$SDK/build-tools/34.0.0"
PLATFORM="$SDK/platforms/android-34/android.jar"

AAPT2="$BT/aapt2.exe"
ZIPALIGN="$BT/zipalign.exe"
APKSIGNER="$BT/apksigner.bat"
D8="$BT/d8.bat"
JAVAC="$JDK/bin/javac.exe"
KEYTOOL="$JDK/bin/keytool.exe"

APP_NAME="etf-quant"
BUILD="$HERE/build"
OUT="$ROOT/dist"

# d8.bat / apksigner.bat 都靠 JAVA_HOME 找 java，必须显式导出
export JAVA_HOME="$JDK"
export PATH="$JDK/bin:$PATH"

say() { printf "\033[36m[build]\033[0m %s\n" "$*"; }
die() { printf "\033[31m[ERR]\033[0m %s\n" "$*" >&2; exit 1; }

for f in "$AAPT2" "$ZIPALIGN" "$APKSIGNER" "$JAVAC"; do
  [ -f "$f" ] || die "缺少构建工具：$f（请检查 SDK/JDK 路径）"
done
[ -f "$PLATFORM" ] || die "缺少 android.jar：$PLATFORM"

# ---------------------------------------------------------------------------
say "1/8 同步前端资源与数据"
ASSETS="$HERE/assets"
rm -rf "$ASSETS"
mkdir -p "$ASSETS/www"
cp -r "$ROOT/mobile/app/assets/www/." "$ASSETS/www/"
[ -f "$ASSETS/www/data.js" ] || die "assets/www/data.js 缺失，请先运行 scripts/export_mobile_data.py"
say "      assets/www: $(find "$ASSETS/www" -type f | wc -l | tr -d ' ') 个文件"

# ---------------------------------------------------------------------------
say "2/8 编译资源"
rm -rf "$BUILD"
mkdir -p "$BUILD/gen" "$BUILD/classes" "$BUILD/dex"
"$AAPT2" compile --dir "$HERE/res" -o "$BUILD/res.zip"

say "3/8 链接清单与资源"
# 版本号只认 AndroidManifest.xml 一处。之前 build.sh 里另写一份 --version-code 4
# 会覆盖清单，改清单不生效、改脚本忘改清单，两边必然漂移。
VCODE=$(sed -n 's/.*android:versionCode="\([0-9]*\)".*/\1/p' "$HERE/AndroidManifest.xml" | head -1)
VNAME=$(sed -n 's/.*android:versionName="\([^"]*\)".*/\1/p' "$HERE/AndroidManifest.xml" | head -1)
[ -n "$VCODE" ] || die "无法从 AndroidManifest.xml 解析 versionCode"
say "      versionCode=$VCODE versionName=$VNAME"
"$AAPT2" link \
  -o "$BUILD/base.apk" \
  -I "$PLATFORM" \
  --manifest "$HERE/AndroidManifest.xml" \
  -R "$BUILD/res.zip" \
  -A "$ASSETS" \
  --java "$BUILD/gen" \
  --min-sdk-version 24 \
  --target-sdk-version 34 \
  --version-code "$VCODE" \
  --version-name "$VNAME" \
  --auto-add-overlay

# ---------------------------------------------------------------------------
say "4/8 编译 Java"
SRC_FILES=$(find "$HERE/src" "$BUILD/gen" -name '*.java')
if ! "$JAVAC" -encoding UTF-8 -source 17 -target 17 -nowarn \
      -classpath "$PLATFORM" -d "$BUILD/classes" $SRC_FILES > "$BUILD/javac.log" 2>&1; then
  cat "$BUILD/javac.log"
  die "javac 编译失败"
fi
[ -d "$BUILD/classes/com" ] || die "javac 未产出 class 文件"

say "5/8 dex"
if ! "$D8" --lib "$PLATFORM" --min-api 24 --release \
      --output "$BUILD/dex" \
      $(find "$BUILD/classes" -name '*.class') > "$BUILD/d8.log" 2>&1; then
  cat "$BUILD/d8.log"
  die "d8 失败"
fi
[ -f "$BUILD/dex/classes.dex" ] || die "d8 未产出 classes.dex"

# ---------------------------------------------------------------------------
say "6/8 打包"
# classes.dex 必须放在 APK 根目录
python - "$BUILD/base.apk" "$BUILD/dex/classes.dex" <<'PY'
import shutil, sys, zipfile
apk, dex = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(apk, "a", zipfile.ZIP_DEFLATED) as z:
    z.write(dex, "classes.dex")
print("      已写入 classes.dex")
PY

# 签名会破坏对齐，必须先 align 再 sign
"$ZIPALIGN" -f -p 4 "$BUILD/base.apk" "$BUILD/aligned.apk"

say "7/8 签名"
KS="$HERE/etfquant.keystore"
if [ ! -f "$KS" ]; then
  say "      生成签名密钥（仅首次）"
  "$KEYTOOL" -genkeypair -v \
    -keystore "$KS" -alias etfquant \
    -keyalg RSA -keysize 2048 -validity 10000 \
    -storepass etfquant -keypass etfquant \
    -dname "CN=ETF Quant, OU=Personal, O=ETFQuant, L=Local, C=CN" >/dev/null 2>&1
fi

mkdir -p "$OUT"
"$APKSIGNER" sign \
  --ks "$KS" --ks-key-alias etfquant \
  --ks-pass pass:etfquant --key-pass pass:etfquant \
  --out "$OUT/$APP_NAME.apk" \
  "$BUILD/aligned.apk"

say "8/8 校验"
"$APKSIGNER" verify --print-certs "$OUT/$APP_NAME.apk" | head -4
SIZE=$(python -c "import os,sys;print(round(os.path.getsize(sys.argv[1])/1048576,2))" "$OUT/$APP_NAME.apk")
say "完成 → dist/$APP_NAME.apk  (${SIZE} MB)"
