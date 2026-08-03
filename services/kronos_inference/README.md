# خدمة Kronos للاستدلال المحلي

خدمة HTTP صغيرة تفصل PyTorch وأوزان Kronos في عملية فرعية عن حلقة Superx1.
تستخدم مكتبة بايثون القياسية للـHTTP، ولا تشغّل CORS أو وضع debug، وتستمع
افتراضيًا على `127.0.0.1` فقط. لا تنفذ الخدمة صفقات؛ تعيد عوائد متوقعة
لاستخدامها في وضع Shadow والتقييم.

## التجهيز

استخدم checkout خارجيًا حتى يبقى كود Kronos وترخيصه منفصلين:

```bash
git clone https://github.com/shiyu-coder/Kronos.git /opt/kronos
git -C /opt/kronos checkout 67b630e67f6a18c9e9be918d9b4337c960db1e9a
python3 -m venv .venv-kronos
. .venv-kronos/bin/activate
python3 -m pip install -r services/kronos_inference/requirements.txt
export KRONOS_REPO_PATH=/opt/kronos
export KRONOS_API_TOKEN='غيّر-هذا-السر'
python3 -m services.kronos_inference
```

المتطلبات تثبّت `torch==2.13.0` بدل 2.5.1 القديم بسبب ثغرات تحميل أوزان
معروفة. مصدر Kronos نفسه يطلب `torch>=2.0`، واختبارات العقد لا تعتمد على
إصدار داخلي خاص بـ2.5. يستخدم Render وصورة Docker الـwheel الرسمي
`2.13.0+cpu` لنفس الإصدار، كي لا تُنزّل مكتبات CUDA غير المستخدمة؛ ويقبل
حارس الجاهزية هذه اللاحقة فقط، مع رفض أي build محلي آخر.

يجب الاحتفاظ بملف `LICENSE` الموجود في checkout الخارجي عند النشر أو إعادة
التوزيع. راجع أيضًا `THIRD_PARTY_NOTICES.md`.

## الإعدادات

| المتغير | الافتراضي | الغرض |
|---|---:|---|
| `KRONOS_REPO_PATH` | لا يوجد | مسار checkout الخارجي؛ يلزم قبل أول توقع |
| `KRONOS_SOURCE_REVISION` | `67b630e67f6a18c9e9be918d9b4337c960db1e9a` | هوية commit كود Kronos المثبت |
| `KRONOS_API_TOKEN` | لا يوجد | عند ضبطه يصبح Bearer إلزاميًا لـ`/ready` و`/v1/forecast` |
| `KRONOS_HOST` | `127.0.0.1` | عنوان الربط؛ أي عنوان غير loopback يتطلب `KRONOS_API_TOKEN` |
| `KRONOS_PORT` | `8080` | منفذ HTTP |
| `KRONOS_MAX_BODY_BYTES` | `1048576` | أكبر جسم طلب |
| `KRONOS_REQUEST_TIMEOUT_SECONDS` | `15` | مهلة قراءة جسم الطلب |
| `KRONOS_MAX_REQUEST_THREADS` | `16` | الحد الصلب لخيوط معالجة اتصالات HTTP المتزامنة |
| `KRONOS_MIN_LOOKBACK` | `32` | أقل عدد شموع تاريخية |
| `KRONOS_MAX_CONTEXT` | `512` | أكبر lookback وسياق predictor |
| `KRONOS_MAX_PRED_LEN` | `120` | أكبر عدد طوابع مستقبلية |
| `KRONOS_MAX_HORIZONS` | `32` | أكبر عدد آفاق مطلوبة |
| `KRONOS_DEVICE` | اكتشاف تلقائي | مثل `cpu` أو `cuda:0` أو `mps` |
| `KRONOS_MARKET_TIMEZONE` | `America/New_York` | المنطقة التي تُحوّل إليها طوابع UTC قبل Kronos |

القيم الافتراضية المثبتة للأوزان:

- النموذج: `NeoQuasar/Kronos-small`
- مراجعة النموذج: `901c26c1332695a2a8f243eb2f37243a37bea320`
- tokenizer: `NeoQuasar/Kronos-Tokenizer-base`
- مراجعته: `0e0117387f39004a9016484a186a908917e22426`

يمكن تغيير المعرّفات والمراجعات صراحةً عبر `KRONOS_MODEL_ID` و
`KRONOS_MODEL_REVISION` و`KRONOS_TOKENIZER_ID` و`KRONOS_TOKENIZER_REVISION`،
لكن يجب تسجيل أي تغيير مع نتائج المعايرة كي لا تختلط التجارب.
تُرفض القيم المتحركة مثل `main` ووسوم الإصدارات؛ يجب أن تكون المراجعات الثلاث
commit SHA ثابتًا من 40 خانة hex.

تعليمات التجهيز أعلاه تثبّت كود Kronos نفسه على commit
`67b630e67f6a18c9e9be918d9b4337c960db1e9a`. وإذا احتاج النشر الاستماع على
`0.0.0.0` (كما في بعض منصات الاستضافة) فلن تبدأ الخدمة دون
`KRONOS_API_TOKEN`؛ ضعها خلف HTTPS ولا تعرض منفذها مباشرة.

## عقد HTTP

`GET /health` فحص خفيف لا يحمل PyTorch أو الأوزان، وهو غير محمي كي يصل إليه
فاحص التشغيل. يبقى `200 status=ok` ما دامت العملية حية، والحقل `model_loaded`
يوضح هل نجح أول استدلال في تحميل النموذج. ويعرض `kronos_revision` هوية مصدر
Kronos المثبتة دون فحص القرص أو الاعتمادات.

`GET /ready` فحص جاهزية مستقل لا يحمل الأوزان: يتحقق من ضبط
`KRONOS_REPO_PATH` ووجود `model/kronos.py` وإمكانية العثور على كل اعتمادات
التشغيل ومطابقة **نسخها الدقيقة** لملف `requirements.txt` (`numpy` و`pandas`
و`torch` و`einops` و`huggingface_hub`
و`safetensors` و`tqdm`)، وأن Git HEAD يطابق `KRONOS_SOURCE_REVISION` ولا توجد
تعديلات أو ملفات غير متتبعة/متجاهلة داخل checkout كله (مع استثناء cache
بايثون و`.git`). يمنع ذلك ملفًا مثل `torch.py` في الجذر من حجب الاعتماد
الحقيقي عند إدخال checkout في `sys.path`. لذلك يجب أن يتوفر أمر `git` في بيئة
الخدمة. يعيد `200 status=ready` عند النجاح، أو
`503 status=not_ready` برسالة عامة لا تكشف المسارات أو تفاصيل البيئة. تُخزّن
النتيجة 30 ثانية حتى لا يعيد كل probe تشغيل أوامر Git. وعند ضبط
`KRONOS_API_TOKEN` يتطلب `/ready` ترويسة Bearer أيضًا؛ يبقى `/health` عامًا
وخفيفًا. يعاد فحص تطابق النسخ قبل أول تحميل حتى لا يتجاوز POST مباشر حارس
الجاهزية.

`POST /v1/forecast` يقبل `Content-Type: application/json` و`Content-Length`
فقط. وعند ضبط السر يجب إرسال:

```text
Authorization: Bearer غيّر-هذا-السر
```

يشغّل الخادم افتراضيًا 16 handler متزامنًا كحد أقصى عبر
`KRONOS_MAX_REQUEST_THREADS`. ولأن استدلال النموذج متسلسل لحماية الذاكرة، إذا
وصل توقع صالح أثناء تنفيذ توقع آخر تعيد الخدمة `HTTP 429` مع رمز الخطأ
`busy` فورًا بدل إبقاء الاتصال منتظرًا؛ على العميل التخطي أو إعادة المحاولة
بتأخير محدود. أما الاتصالات التي تتجاوز حد الخيوط نفسه فتُغلق قبل إنشاء
handler جديد لحماية العملية من تراكم الاتصالات البطيئة.

مثال مختصر للطلب؛ يجب أن يكون عدد `bars` بين 32 و512 افتراضيًا:

```json
{
  "ticker": "AAPL",
  "bars": [
    {
      "timestamp": "2026-08-03T09:30:00-04:00",
      "open": 210.0,
      "high": 211.2,
      "low": 209.8,
      "close": 210.9,
      "volume": 120000,
      "amount": 25272000
    }
  ],
  "future_timestamps": [
    "2026-08-03T09:35:00-04:00",
    "2026-08-03T09:40:00-04:00"
  ],
  "horizons": [1, 2]
}
```

كل timestamp يجب أن يكون ISO 8601 مع إزاحة زمنية، وأن تكون القوائم متزايدة
بلا تكرار. يبدأ أول طابع مستقبلي بعد آخر bar بخمس دقائق، وتبقى الطوابع
المستقبلية على cadence خمس دقائق ثابت. تُشتق `lookback` من عدد `bars` و`pred_len` من عدد
`future_timestamps`. الحقل `amount` اختياري؛ عند غيابه تحسبه الخدمة من OHLCV.
تتحقق الخدمة كذلك من محدودية الأرقام ومن علاقات OHLC ومن أن كل horizon داخل
`pred_len`. يحوّل الخادم الطوابع إلى منطقة السوق قبل تمرير حقول الساعة واليوم
إلى Kronos؛ مثلًا تصبح `13:30Z` في أغسطس `09:30` بنيويورك.

استجابة ناجحة:

```json
{
  "status": "ok",
  "returns_pct": {"6": 1.245, "12": -0.31},
  "model": "NeoQuasar/Kronos-small",
  "model_revision": "901c26c1332695a2a8f243eb2f37243a37bea320",
  "tokenizer": "NeoQuasar/Kronos-Tokenizer-base",
  "tokenizer_revision": "0e0117387f39004a9016484a186a908917e22426",
  "kronos_revision": "67b630e67f6a18c9e9be918d9b4337c960db1e9a",
  "service_revision": "sha256-of-service-source",
  "market_timezone": "America/New_York",
  "device": "cuda:0",
  "max_context": 512,
  "lookback": 400,
  "pred_len": 18,
  "latency_ms": 842.17
}
```

`returns_pct["6"]` هي **نسبة مئوية** بين إغلاق التوقع عند الشمعة السادسة
وآخر إغلاق حقيقي؛ ليست كسرًا عشريًا. الخدمة تعيد الآفاق المطلوبة فقط.
`service_revision` بصمة SHA-256 لكود غلاف الخدمة وملف الاعتمادات المثبتة
`requirements.txt`، و`device` هو الجهاز الفعلي بعد الاكتشاف التلقائي، لا مجرد
قيمة الإعداد. تُضم هذه الحقول و`max_context` إلى هوية تجربة Superx1 كي لا
تختلط نتائج تشغيلات مختلفة.

## Docker مستقل (اختياري)

يبني الـDockerfile checkout Kronos على commit المثبت، ويشغّل الخدمة كمستخدم
غير root. من جذر Superx1:

```bash
docker build -f services/kronos_inference/Dockerfile -t superx1-kronos .
docker run --rm -p 8080:8080 \
  -e KRONOS_API_TOKEN='سر-طويل-عشوائي' superx1-kronos
```

يستخدم `render.yaml` الجذري افتراضيًا worker واحدًا على Standard (2GB).
يشغّل `runner_scanner.render_supervisor` هذه الخدمة على loopback بجانب الماسح،
ويخبز `build_runtime` المصدر والأوزان في artifact مع تحقق offline. القياس
الفعلي على ARM64/CPU بالإعداد الإنتاجي الكامل (512 شمعة، توقع 18) بلغ
445.81MiB RSS و2.35ث باردًا و0.93ث دافئًا؛ لذلك خطة 512MiB لا تترك هامشًا
إنتاجيًا آمنًا عند جمع العمليتين.

يبقى Dockerfile خيارًا لعزل Kronos في خدمة ثانية إذا قُبلت تكلفتها لاحقًا.
في هذا الوضع الأوزان والـtokenizer مخبوزة داخل الصورة، ويعمل runtime بـ
`HF_HUB_OFFLINE=1` ولا يحتاج قرص cache دائم.

الملف `render.kronos.yaml.example` بديل اختياري لنشر الخدمة وحدها؛ عند استخدامه
اضبط `KRONOS_API_TOKEN` في لوحة Render، ثم اربط العامل يدويًا بعنوان HTTPS
للخدمة والتوكن نفسه.

## الاختبارات

لا تستورد الاختبارات pandas أو torch ولا تنزّل أوزانًا:

```bash
python3 -m unittest discover -s services/kronos_inference/tests -v
```
