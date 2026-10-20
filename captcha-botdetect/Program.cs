using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Imaging;
using System.Drawing.Text;

var builder = WebApplication.CreateBuilder(args);
var app = builder.Build();

var groups = new Dictionary<string, GroupPreset>
{
    ["g_70x23"]    = new(70,   23,  "ABCDEFGHJKLMNOPQRSTUVWXYZ",                           4, 4),
    ["g_80x30"]    = new(80,   30,  "0123456789",                                           4, 5),
    ["g_120x38"]   = new(120,  38,  "0123456789",                                           4, 5),
    ["g_120x40"]   = new(120,  40,  "23456789Zabcdefghjkmnpqrtuwxyz",                       5, 6),
    ["g_150x30"]   = new(150,  30,  "ABCDEFGHIJKLMNOPQRSTUVWXYZ",                           5, 6),
    ["g_150x34"]   = new(150,  34,  "0123456789",                                           4, 6),
    ["g_150x39"]   = new(150,  39,  "0123456789abcdefghijklmnopqrstuvwxyz",                 3, 4),
    ["g_160x60"]   = new(160,  60,  "2345678abcdefgmnpwxy",                                 4, 4),
    ["g_160x75"]   = new(160,  75,  "0123456789abcdefghijklmnopqrstuvwxyz",                 5, 6),
    ["g_180x50"]   = new(180,  50,  "0123456789abcdefghijklmnopqrstuvwxyz",                 3, 6),
    ["g_180x64"]   = new(180,  64,  "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ",                 5, 5),
    ["g_194x35"]   = new(194,  35,  "0123456789",                                           4, 7),
    ["g_200x70"]   = new(200,  70,  "ABCDEFGHIJKLMNOPQRSTUVWYZ",                            1, 5),
    ["g_200x80"]   = new(200,  80,  "23456789abcdefghijklmnopqrstuvxyz",                    5, 5),
    ["g_250x40"]   = new(250,  40,  "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ",                 5, 10),
    ["g_300x75"]   = new(300,  75,  "0123456789",                                           5, 5),
    ["g_600x120"]  = new(600,  120, "23456789ABCDEFGHJKMNPQRSTVWXYZ",                       6, 7),
    ["g_1500x350"] = new(1500, 350, "0123456789",                                           4, 4),
};

app.MapGet("/", () => Results.Content(IndexHtml.Value, "text/html"));

app.MapGet("/api/groups", () =>
    Results.Json(groups.Select(g => new {
        id = g.Key, width = g.Value.Width, height = g.Value.Height,
        charset = g.Value.Charset, labelMin = g.Value.LabelMin, labelMax = g.Value.LabelMax,
    }))
);

app.MapGet("/api/preview/{groupId}", (string groupId) =>
{
    if (!groups.TryGetValue(groupId, out var preset))
        return Results.NotFound($"Group {groupId} not found");
    var (label, bytes) = CaptchaRenderer.Generate(preset);
    return Results.Json(new { label, image = Convert.ToBase64String(bytes) });
});

app.MapPost("/api/generate", async (HttpRequest req) =>
{
    var body = await req.ReadFromJsonAsync<GenerateRequest>();
    if (body == null) return Results.BadRequest("Invalid request");
    if (!groups.TryGetValue(body.GroupId, out var preset))
        return Results.NotFound($"Group {body.GroupId} not found");

    var outputDir = body.OutputDir
        ?? Path.Combine(Directory.GetCurrentDirectory(), "..", "generated", body.GroupId);
    Directory.CreateDirectory(outputDir);

    int count = Math.Clamp(body.Count, 1, 50000);
    var samples = new List<object>();

    for (int i = 0; i < count; i++)
    {
        var (label, bytes) = CaptchaRenderer.Generate(preset);
        var fileName = $"{label}_{i:D5}.png";
        await File.WriteAllBytesAsync(Path.Combine(outputDir, fileName), bytes);
        if (i < 10) samples.Add(new { label, file = fileName });
    }

    return Results.Json(new { group = body.GroupId, count, outputDir, samples });
});

app.Run("http://localhost:5001");

// ============================================================
static class CaptchaRenderer
{
    static readonly Random Rng = new();
    static readonly string[] Fonts = {
        "Arial", "Verdana", "Tahoma", "Times New Roman",
        "Georgia", "Courier New", "Trebuchet MS", "Comic Sans MS",
    };

    public static (string label, byte[] png) Generate(GroupPreset p)
    {
        int len = Rng.Next(p.LabelMin, p.LabelMax + 1);
        var chars = new char[len];
        for (int i = 0; i < len; i++)
            chars[i] = p.Charset[Rng.Next(p.Charset.Length)];
        string label = new(chars);
        return (label, Render(label, p.Width, p.Height));
    }

    public static byte[] Render(string text, int w, int h)
    {
        using var bmp = new Bitmap(w, h, PixelFormat.Format32bppArgb);
        using var g = Graphics.FromImage(bmp);
        g.SmoothingMode = SmoothingMode.AntiAlias;
        g.TextRenderingHint = TextRenderingHint.AntiAlias;

        DrawBg(g, w, h);
        DrawLines(g, w, h, Rng.Next(3, 7), 150, 220);
        DrawDots(g, w, h, w * h / 50);
        DrawText(g, text, w, h);
        DrawLines(g, w, h, Rng.Next(1, 4), 80, 180);

        using var distorted = WaveDistort(bmp);
        using var ms = new MemoryStream();
        distorted.Save(ms, ImageFormat.Png);
        return ms.ToArray();
    }

    static void DrawBg(Graphics g, int w, int h)
    {
        var c1 = RCol(200, 250);
        var c2 = RCol(200, 250);
        var dir = Rng.Next(3) switch
        {
            0 => new Point(w, 0),
            1 => new Point(0, h),
            _ => new Point(w, h),
        };
        using var brush = new LinearGradientBrush(Point.Empty, dir, c1, c2);
        g.FillRectangle(brush, 0, 0, w, h);
    }

    static void DrawText(Graphics g, string text, int w, int h)
    {
        int n = text.Length;
        float cellW = (float)w / (n + 1);
        float startX = cellW * 0.5f;
        float fontSize = Math.Max(8f, h * RF(0.45f, 0.7f));

        for (int i = 0; i < n; i++)
        {
            string ch = text[i].ToString();
            var style = Rng.Next(3) == 0 ? FontStyle.Bold : FontStyle.Regular;
            using var font = new Font(Fonts[Rng.Next(Fonts.Length)], fontSize, style, GraphicsUnit.Pixel);
            using var brush = new SolidBrush(RCol(10, 130));
            var sz = g.MeasureString(ch, font);

            float cx = startX + i * cellW;
            float cy = (h - sz.Height) / 2f + RF(-h * 0.12f, h * 0.12f);
            float angle = RF(-30f, 30f);

            var state = g.Save();
            g.TranslateTransform(cx + sz.Width / 2, cy + sz.Height / 2);
            g.RotateTransform(angle);
            g.DrawString(ch, font, brush, -sz.Width / 2, -sz.Height / 2);
            g.Restore(state);
        }
    }

    static void DrawLines(Graphics g, int w, int h, int count, int lo, int hi)
    {
        for (int i = 0; i < count; i++)
        {
            using var pen = new Pen(RCol(lo, hi), RF(0.8f, 2f));
            if (Rng.Next(2) == 0)
                g.DrawLine(pen, Rng.Next(w), Rng.Next(h), Rng.Next(w), Rng.Next(h));
            else
                g.DrawBezier(pen,
                    new PointF(Rng.Next(w), Rng.Next(h)), new PointF(Rng.Next(w), Rng.Next(h)),
                    new PointF(Rng.Next(w), Rng.Next(h)), new PointF(Rng.Next(w), Rng.Next(h)));
        }
    }

    static void DrawDots(Graphics g, int w, int h, int count)
    {
        for (int i = 0; i < count; i++)
        {
            using var brush = new SolidBrush(RCol(80, 220));
            g.FillEllipse(brush, Rng.Next(w), Rng.Next(h), Rng.Next(1, 3), Rng.Next(1, 3));
        }
    }

    static Bitmap WaveDistort(Bitmap src)
    {
        int w = src.Width, h = src.Height;
        var dst = new Bitmap(w, h, src.PixelFormat);
        double ax = RF(1.5f, Math.Min(4, h * 0.07f));
        double ay = RF(1f, Math.Min(3, w * 0.02f));
        double fx = RF(0.03f, 0.08f), fy = RF(0.03f, 0.08f);
        double px = Rng.NextDouble() * Math.PI * 2;
        double py = Rng.NextDouble() * Math.PI * 2;

        for (int y = 0; y < h; y++)
        for (int x = 0; x < w; x++)
        {
            int sx = Math.Clamp(x + (int)(ay * Math.Sin(fy * y + py)), 0, w - 1);
            int sy = Math.Clamp(y + (int)(ax * Math.Sin(fx * x + px)), 0, h - 1);
            dst.SetPixel(x, y, src.GetPixel(sx, sy));
        }
        return dst;
    }

    static Color RCol(int lo, int hi) =>
        Color.FromArgb(Rng.Next(lo, hi), Rng.Next(lo, hi), Rng.Next(lo, hi));
    static float RF(float lo, float hi) =>
        lo + (float)Rng.NextDouble() * (hi - lo);
    static float RF(float lo, double hi) =>
        lo + (float)Rng.NextDouble() * ((float)hi - lo);
}

record GroupPreset(int Width, int Height, string Charset, int LabelMin, int LabelMax);
record GenerateRequest(string GroupId, int Count, string? OutputDir);

// ============================================================
static class IndexHtml
{
    public const string Value = """
<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Captcha Generator</title>
<style>
:root{--bg:#0f172a;--surface:#1e293b;--border:#334155;--text:#e2e8f0;--muted:#94a3b8;--accent:#3b82f6;--green:#22c55e}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:system-ui,sans-serif;background:var(--bg);color:var(--text);min-height:100vh;padding:40px 16px;display:flex;flex-direction:column;align-items:center}
h1{font-size:1.5rem;margin-bottom:24px}
.container{width:100%;max-width:620px}
label{font-size:13px;color:var(--muted);display:block;margin-bottom:4px}
select,input[type=number],input[type=text]{width:100%;padding:10px 12px;border-radius:8px;border:1px solid var(--border);background:var(--surface);color:var(--text);font-size:14px;margin-bottom:12px}
.row{display:flex;gap:12px}.row>*{flex:1}
.btn{display:block;width:100%;padding:12px;border:none;border-radius:8px;background:var(--accent);color:#fff;font-size:15px;cursor:pointer;margin-bottom:8px}
.btn:disabled{opacity:.5;cursor:not-allowed}
.btn-green{background:var(--green)}
.preview{display:flex;flex-wrap:wrap;gap:8px;margin-top:16px}
.preview-item{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:8px;text-align:center}
.preview-item img{max-width:100%;height:auto;background:#fff;border-radius:4px}
.preview-item .val{font:700 15px ui-monospace,monospace;letter-spacing:2px;color:var(--green);margin-top:4px}
#status{text-align:center;color:var(--muted);font-size:13px;margin-top:12px;min-height:20px}
.info{font-size:12px;color:var(--muted);background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:12px;margin-bottom:16px}
</style>
</head>
<body>
<h1>Captcha Generator</h1>
<div class="container">
  <div class="info">Chọn group → Preview để xem thử → Generate để lưu ảnh. File name: <code>&lt;label&gt;_&lt;index&gt;.png</code></div>
  <label>Group</label>
  <select id="group"></select>
  <div class="row">
    <div><label>Số lượng</label><input id="count" type="number" value="1000" min="1" max="50000"/></div>
    <div><label>Output dir (optional)</label><input id="outdir" type="text" placeholder="Default: ../generated/<group>"/></div>
  </div>
  <button class="btn" onclick="doPreview()">Preview (6 ảnh)</button>
  <button class="btn btn-green" onclick="doGenerate()">Generate & Save</button>
  <div id="status"></div>
  <div class="preview" id="preview"></div>
</div>
<script>
async function init(){
  const r=await fetch('/api/groups');const data=await r.json();
  const sel=document.getElementById('group');
  data.forEach(g=>{const o=document.createElement('option');o.value=g.id;
    o.textContent=`${g.id}  (${g.width}x${g.height}, chars=${g.charset.length}, len=${g.labelMin}-${g.labelMax})`;
    sel.appendChild(o);});
}
init();

async function doPreview(){
  const gid=document.getElementById('group').value;
  const div=document.getElementById('preview');div.innerHTML='';
  document.getElementById('status').textContent='Generating preview...';
  for(let i=0;i<6;i++){
    const r=await fetch('/api/preview/'+gid);const d=await r.json();
    div.innerHTML+=`<div class="preview-item"><img src="data:image/png;base64,${d.image}"/><div class="val">${d.label}</div></div>`;
  }
  document.getElementById('status').textContent='';
}

async function doGenerate(){
  const gid=document.getElementById('group').value;
  const count=parseInt(document.getElementById('count').value)||1000;
  const outdir=document.getElementById('outdir').value.trim()||null;
  const st=document.getElementById('status');
  st.textContent=`Generating ${count} images...`;
  document.querySelectorAll('.btn').forEach(b=>b.disabled=true);
  try{
    const r=await fetch('/api/generate',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({groupId:gid,count:count,outputDir:outdir})});
    const d=await r.json();
    st.textContent=`Done! ${d.count} images → ${d.outputDir}`;
  }catch(e){st.textContent='Error: '+e.message;}
  finally{document.querySelectorAll('.btn').forEach(b=>b.disabled=false);}
}
</script>
</body>
</html>
""";
}
