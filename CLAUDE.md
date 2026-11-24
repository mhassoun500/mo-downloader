# MO Downloader — ملف التسليم لمن يكمل التطوير

اقرأ هذا الملف كاملاً قبل أي عمل، ثم `docs/dev-plan.md` (الخطة والمراحل).

## ما هي الأداة

برنامج شخصي لتنزيل الفيديو والصوت، مبني على yt-dlp، بواجهة ويب عربية (RTL) داخل نافذة خاصة به
(pywebview)، يعمل على ويندوز وماك (Apple Silicon وIntel) ولينكس، ومعه إضافة متصفح.

- الإصدار الحالي: **2.6** (رقم الإصدار في ملف `VERSION` فقط — غيّره هناك).
- إضافة المتصفح: **1.4.0** (`browser-extension/manifest.json`).
- المستودع خاص: `mhassoun500/mo-downloader` — الفرع `main`.

## الحالة

| المرحلة | المحتوى | الإصدار |
|---|---|---|
| 1 | التنزيل من مواقع أكثر: سلسلة محاولات (مباشر ← انتحال بصمة المتصفح curl-cffi ← كوكيز المتصفح ← gallery-dl ← البث الملتقط ← البروكسي)، تسجيل البث المباشر، صفحة المواقع المدعومة، تحديث المحرك، تقرير الخطأ، التقاط ترويسات الطلب في الإضافة | 2.5 ✅ |
| 2 | 16 إصلاحاً: حفظ الطابور، إيقاف مؤقت للقوائم والبث المجزّأ، أيقونة الساعة/شريط القوائم، متابعة القنوات حتى آخر فيديو + Shorts والبث، السجل في SQLite، تصنيف الأخطاء، منافذ موحّدة 8765–8774، مساعد native messaging يشغّل البرنامج من الإضافة، فلاتر تمرير التنزيلات، حماية الكوكيز | 2.6 ✅ |
| 3 | مزايا إضافية (التحويل، الفصول، الترجمات، حد السرعة والجدولة، قوالب الأسماء، الإشعارات، الحافظة، الجوال، لغتان، تصدير/استيراد، تحديث ذاتي) | **التالية** |
| 4 | التوزيع (توقيع، سفاري، المتاجر، ملف ماك موحّد، سجل تغييرات) | لاحقاً |

## قواعد العمل (يطلبها صاحب الأداة دائماً)

1. **اكتب له بالعربية دائماً**، حتى الملاحظات القصيرة بين الخطوات وخيارات الأسئلة. وأي كلمة أو مصطلح إنجليزي ضعه **في سطر مستقل لوحده** ثم أكمل العربي في سطر جديد (حتى لا يختل اتجاه النص).
2. كل ملف يُصدر يحمل رقم الإصدار في اسمه:
   `MO-Downloader-X.Y-Setup.exe` و`MO-Downloader-X.Y-mac-arm64.dmg` و`MO-Downloader-X.Y-mac-x64.dmg` و`MO-Downloader-X.Y.zip`.
3. ارفع للمستودع، وابنِ ملفات التثبيت عبر GitHub Actions، وأرسلها له.
4. لا تقل إن شيئاً يعمل إلا بعد تجربته فعلاً، واذكر بوضوح ما لم يُجرَّب على جهاز ويندوز أو ماك حقيقي.
5. أضف لكل ميزة اختباراً آلياً في CI كلما أمكن.
6. ممنوع: تجاوز DRM أو المحتوى المدفوع، البحث عن بروكسي مجاني، خدمات تجاوز الحجب أو VPN. (رُفضت سابقاً ووافق).
7. لا تنسخ كوداً من برنامج Snagit أو غيره (أفكار فقط).
8. نفّذ المراحل بالترتيب، كل مرحلة إصدار مستقل، واسأله قبل الانتقال للتالية.

## بنية المشروع

```
VERSION                    رقم الإصدار (المصدر الوحيد)
src/server.py              الخادم والمنطق كله (Python، ~3200 سطر)
src/page.html              الواجهة (HTML/JS/CSS في ملف واحد)
src/logo128.png            الشعار
src/make.py                يجمع الثلاثة في ملف واحد app/mo_downloader.py ويكتب الإصدار في Info.plist واقرأني
app/mo_downloader.py       الناتج — لا تعدّله يدوياً
browser-extension/         إضافة كروم/إيدج/فايرفوكس (MV3). "key" في manifest يثبّت المعرّف:
                           kfiopiphhapdnaeibmhmdloghbnheeoo (يلزم لمساعد native messaging)
MO Downloader.app/         غلاف ماك لنسخة بايثون (انسخ إليه app/mo_downloader.py و browser-extension بعد البناء)
MO Downloader - Windows.bat, start_linux.sh   مشغّلات نسخة بايثون
build-kit/build.py         بناء PyInstaller + المثبّت (يُشغَّل في CI)
.github/workflows/build.yml  البناء والفحوص لثلاثة أنظمة + الاختبارات
tests/run_tests.py         38 اختباراً شاملاً ضد موقع تجريبي محلي (tests/testsite.py)
tests/ext_test.py          15 اختباراً للإضافة داخل Chromium (Playwright)
tests/test_errors.py       اختبارات تصنيف رسائل الخطأ
docs/dev-plan.md           خطة التطوير والمراحل
```

## الأوامر

```bash
python3 src/make.py app/mo_downloader.py          # بعد أي تعديل في src/
cp app/mo_downloader.py "MO Downloader.app/Contents/Resources/mo_downloader.py"
rm -rf "MO Downloader.app/Contents/Resources/browser-extension" && cp -r browser-extension "MO Downloader.app/Contents/Resources/"

pip install "yt-dlp[default,curl-cffi]" yt-dlp-ejs gallery-dl playwright   # + ffmpeg على النظام
python3 tests/test_errors.py
python3 tests/run_tests.py            # --network يضيف اختبار imgur الحقيقي
python -m playwright install chromium && python3 tests/ext_test.py

# تشغيل يدوي معزول للتجربة
HOME=/tmp/mo-home MD_PORT=8765 MD_NO_WINDOW=1 MD_NO_BROWSER=1 python3 app/mo_downloader.py
```

أوامر خاصة يفهمها البرنامج: `--engine-check FILE` (فحص المحركات)، `--gallery-dl ...` (مع `MO_GDL_OUT`)،
`--background`، ومتغير `MO_SELFTEST=FILE` لفحص النافذة في CI (يكتب `OK gui=... tray=OK reopen=OK`).

## الإصدار وتسليم الملفات

- مساحة «ملفات البناء المؤقتة» (artifacts) في حساب GitHub امتلأت سابقاً وتسببت في فشل الرفع.
  الطريقة المعتمدة الآن: **إنشاء Release بوسم `vX.Y`** — البناء يبدأ تلقائياً ويرفق الملفات الثلاثة بالإصدار
  (خطوة «Attach the files to the Release» في build.yml)، ولا تُحسب من تلك المساحة. نجحت مع v2.6.
- إن لم تكن للجلسة صلاحية إنشاء وسم أو Release، اطلب من صاحب الأداة إنشاءه من صفحة Releases.
- ملف المصدر: `git archive --format=zip --prefix="MO Downloader X.Y/" -o MO-Downloader-X.Y.zip HEAD`.
- عند تغيير رقم الإصدار: عدّل `VERSION`، وأضف قسم «جديد في X.Y» أعلى `اقرأني.txt`، وارفع إصدار الإضافة إن تغيّرت.

## ملاحظات مهمة

- يوتيوب يطلب «تأكيد أنك لست روبوتاً» من خوادم GitHub وخوادم Claude، فلا يمكن التحقق من جودات فوق 720p
  من هناك؛ فحص CI لذلك معلوماتي فقط. على جهاز المستخدم تعيد الأداة المحاولة بكوكيز المتصفح.
- مولّد رموز PO لليوتيوب (bgutil) لم يُضف: تثبيت سكربتاته الخارجية لم يُسمح به في بيئة العمل.
- سفاري لا يدعم native messaging، فيبقى على رابط `mo-downloader://` (رسالة «فتح البرنامج؟»).
- بيانات المستخدم في `~/.media_downloader/` (settings.json، history.db، queue.json، subs، cookies، log.txt).
- لم يُجرَّب شيء على جهاز ويندوز أو ماك حقيقي من قِبل Claude؛ كل الفحوص على أجهزة GitHub ولينكس.
