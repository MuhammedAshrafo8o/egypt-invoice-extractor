# Egypt Invoice Extractor

مكتبة Python بتاخد فواتير (PDF أو صور) وتطلّع بياناتها منظمة، وبعدين **تراجعها أوتوماتيك** زي ما المحاسب بيراجع بعينه، وتجهز ملخص لإقرار ضريبة القيمة المضافة.

A Python library that extracts structured data from Egyptian invoices (PDF / photos), validates it the way an accountant would, and produces a VAT-return summary.

---

## بتعمل إيه؟

| نوع الملف | الطريقة | الدقة |
|---|---|---|
| فاتورة إلكترونية عليها QR من المنظومة | يقرا الـ QR ويحفظ الـ UUID ورابط الفاتورة + يقرا النص | عالية جدًا |
| PDF رقمي (فيه نص) | pdfplumber + محلل قواعد عربي/إنجليزي | عالية |
| PDF ممسوح ضوئيًا أو صورة فاتورة | Claude (قراءة بالذكاء الاصطناعي) | عالية، محتاجة مراجعة للمكتوب بخط اليد |

### البيانات اللي بتطلع
اسم المورد، رقمه الضريبي، اسم العميل ورقمه الضريبي، رقم الفاتورة، التاريخ، العملة، المبلغ قبل الضريبة، نسبة وقيمة الضريبة، الإجمالي.

### طبقة التحقق (أهم جزء)
- الرقم الضريبي لازم يكون ٩ أرقام (ولو ١٤ رقم ينبّه إنه غالبًا رقم قومي).
- الإجمالي = المبلغ + الضريبة.
- نسبة الضريبة منطقية (١٤٪، أو ٥٪، أو صفر).
- التاريخ مش في المستقبل (غالبًا اليوم والشهر متبدلين) ومش قديم أوي.
- الفاتورة مش متكررة في نفس الدفعة.
- الحقول الأساسية موجودة.

كل فاتورة بتاخد حالة:
- ✅ `ok` جاهزة
- 🟡 `review` تحتاج نظرة سريعة
- 🔴 `error` فيها مشكلة لازم تتصلح

---

## التشغيل

```bash
pip install -r requirements.txt

# جرّب على فواتير تجريبية
python samples/make_samples.py
python -m invoice_extractor samples/invoices --out results
```

النتيجة:
- `results/invoices.json`: كل فاتورة بالتفصيل + ملخص الإقرار.
- `results/invoices.csv`: يفتح على Excel بالعربي مباشرة.

### تفعيل قراءة الصور والـ PDF الممسوح

```bash
export ANTHROPIC_API_KEY=sk-ant-...
export ANTHROPIC_MODEL=claude-sonnet-5-5   # اختياري
python -m invoice_extractor path/to/invoices/
```

من غير المفتاح، الصور بتتعلّم إنها محتاجة AI، والباقي بيشتغل عادي. ولو عايز توقف الـ AI خالص استخدم `--no-ai`.

### الاستخدام من الكود

```python
from invoice_extractor import process_many, vat_summary

invoices = process_many(["inv1.pdf", "photo.jpg"])
for inv in invoices:
    print(inv.status, inv.supplier_tax_id, inv.total, [i.message for i in inv.issues])

print(vat_summary(invoices))
```

---

## هيكل المشروع

```
invoice_extractor/
  pipeline.py          # يحدد نوع الملف ويوجهه للمستخرج المناسب
  validation.py        # طبقة التحقق
  normalize.py         # أرقام عربية، تواريخ، توحيد الحروف
  models.py            # Invoice / Issue
  cli.py               # سطر الأوامر
  extractors/
    text_parser.py     # قواعد استخراج الحقول من النص (عربي + إنجليزي)
    pdf_text.py        # قراءة نص الـ PDF
    qr.py              # قراءة QR وتمييز فواتير المنظومة
    ai_vision.py       # Claude API (من غير SDK)
samples/make_samples.py  # يولّد فواتير تجريبية
tests/                   # python -m unittest discover tests
```

## الاختبارات

```bash
python -m unittest discover tests
```

---

## الخطوات الجاية

- [ ] ربط مع API منظومة الفاتورة الإلكترونية لسحب بيانات الفاتورة بالـ UUID (محتاج بيانات دخول الممول).
- [ ] تجربة الـ AI على فواتير حقيقية بالعربي والمكتوبة بخط اليد، وقياس الدقة.
- [ ] تحسين قراءة الـ PDF العربي اللي النص بيطلع منه معكوس (RTL).
- [ ] تقسيم النتائج حسب العميل والشهر.
- [ ] تصدير بصيغة نموذج إقرار القيمة المضافة.
- [ ] استقبال الفواتير من واتساب (n8n / WhatsApp Cloud API).
- [ ] لوحة للمحاسب للمراجعة والتعديل.

> ⚠️ النتائج أداة مساعدة، والمحاسب لازم يراجع قبل تقديم أي إقرار. القواعد الضريبية (النسب والمواعيد) بتتغير، فراجعها مع مصلحة الضرائب.
