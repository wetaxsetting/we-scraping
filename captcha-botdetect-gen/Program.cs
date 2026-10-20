using System;
using System.Collections;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Imaging;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Web;
using System.Web.Hosting;
using System.Web.SessionState;
using BotDetect;

namespace BotDetectGen
{
    class Program
    {
        static readonly ImageStyle[] AllStyles = (ImageStyle[])Enum.GetValues(typeof(ImageStyle));

        static readonly Dictionary<string, GroupPreset> Groups = new Dictionary<string, GroupPreset>
        {
            ["g_70x23"]    = new GroupPreset(70,   23,  "ABCDEFGHJKLMNOPQRSTUVWXYZ",                        4, 4),
            ["g_80x30"]    = new GroupPreset(80,   30,  "0123456789",                                        4, 5),
            ["g_120x38"]   = new GroupPreset(120,  38,  "0123456789",                                        4, 5),
            ["g_120x40"]   = new GroupPreset(120,  40,  "2345678abcdefghkmnprwxy",                            4, 4),
            ["g_150x30"]   = new GroupPreset(150,  30,  "ABCDEFGHIJKLMNOPQRSTUVWXYZ",                        5, 6),
            ["g_150x34"]   = new GroupPreset(150,  34,  "0123456789",                                        4, 6),
            ["g_150x39"]   = new GroupPreset(150,  39,  "0123456789abcdefghijklmnopqrstuvwxyz",              3, 4),
            ["g_160x60"]   = new GroupPreset(160,  60,  "2345678abcdefgmnpwxy",                              4, 4),
            ["g_160x75"]   = new GroupPreset(160,  75,  "0123456789abcdefghijklmnopqrstuvwxyz",              5, 6),
            ["g_180x50_vnisc"] = new GroupPreset(180, 50, "023456789ABCDEFGHIJKLMNPQRSTUVWXYZabcdefghijklmnpqrstuvwxyz", 5, 6),
            ["g_180x50_tax24"] = new GroupPreset(180, 50, "02345678CKXZabcdefghkmnopqrwxy",                  5, 6),
            ["g_180x64"]   = new GroupPreset(180,  64,  "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ",              5, 5),
            ["g_194x35"]   = new GroupPreset(194,  35,  "0123456789",                                        4, 7),
            ["g_200x70"]   = new GroupPreset(200,  70,  "ABCDEFGHIJKLMNOPQRSTUVWYZ",                         1, 5),
            ["g_200x80"]   = new GroupPreset(200,  80,  "23456789abcdefghijklmnopqrstuvxyz",                 5, 5),
            ["g_250x40"]   = new GroupPreset(250,  40,  "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ",              5, 10),
            ["g_300x75"]   = new GroupPreset(300,  75,  "0123456789",                                        5, 5),
            ["g_600x120"]  = new GroupPreset(600,  120, "23456789ABCDEFGHJKMNPQRSTVWXYZ",                    6, 7),
            ["g_1500x350"] = new GroupPreset(1500, 350, "0123456789",                                        4, 4),
        };

        static readonly Random Rng = new Random();

        // Cached reflection fields
        static readonly FieldInfo CcField = typeof(CaptchaBase).GetField("_codeCollection",
            BindingFlags.NonPublic | BindingFlags.Instance);
        static FieldInfo _dictField;
        static FieldInfo _codeTextField;

        // Cached persistence methods (resolved once)
        static MethodInfo _persistCodeCollectionMethod;
        static MethodInfo _persistImageSizeMethod;
        static MethodInfo _persistImageFormatMethod;

        static void Main(string[] args)
        {
            if (args.Length == 0 || args[0] == "--help" || args[0] == "-h")
            {
                PrintUsage();
                return;
            }

            if (args[0] == "list")
            {
                ListGroups();
                return;
            }

            SetupHttpContext();
            ResolvePersistenceMethods();

            string groupId = args[0];
            int count = args.Length > 1 ? int.Parse(args[1]) : 100;
            string outputDir = args.Length > 2 ? args[2] : null;
            string styleName = args.Length > 3 ? args[3] : null;

            if (groupId == "all")
            {
                foreach (var kv in Groups.OrderBy(x => x.Key))
                {
                    var dir = outputDir != null
                        ? Path.Combine(outputDir, kv.Key)
                        : Path.Combine(Directory.GetCurrentDirectory(), "..", "generated", kv.Key);
                    GenerateGroup(kv.Key, kv.Value, count, dir, styleName);
                }
            }
            else if (Groups.TryGetValue(groupId, out var preset))
            {
                outputDir = outputDir ?? Path.Combine(Directory.GetCurrentDirectory(), "..", "generated", groupId);
                GenerateGroup(groupId, preset, count, outputDir, styleName);
            }
            else
            {
                Console.Error.WriteLine($"Unknown group: {groupId}. Use 'list' to see available.");
            }
        }

        static void SetupHttpContext()
        {
            var appDir = AppDomain.CurrentDomain.BaseDirectory;
            var request = new SimpleWorkerRequest("/", appDir, "default.aspx", "", new StringWriter());
            var context = new HttpContext(request);
            var sessionState = new HttpSessionStateContainer(
                "s-" + Guid.NewGuid().ToString("N"),
                new SessionStateItemCollection(),
                new HttpStaticObjectsCollection(),
                20, true, HttpCookieMode.UseDeviceProfile,
                SessionStateMode.InProc, false);
            SessionStateUtility.AddHttpSessionStateToContext(context, sessionState);
            HttpContext.Current = context;
        }

        static void ResolvePersistenceMethods()
        {
            var pt = typeof(BotDetect.Web.CaptchaPersistence);
            foreach (var m in pt.GetMethods(BindingFlags.NonPublic | BindingFlags.Static))
            {
                var p = m.GetParameters();
                if (p.Length != 2 || p[0].ParameterType != typeof(string) || m.ReturnType != typeof(void))
                    continue;
                if (p[1].ParameterType == typeof(CodeCollection))
                    _persistCodeCollectionMethod = m;
                else if (p[1].ParameterType == typeof(ImageSize))
                    _persistImageSizeMethod = m;
                else if (p[1].ParameterType == typeof(BotDetect.ImageFormat))
                    _persistImageFormatMethod = m;
            }
        }

        static string ReadActualCode(object codeCollection, string instanceId)
        {
            if (codeCollection == null) return null;
            if (_dictField == null)
                _dictField = codeCollection.GetType().GetField("_collection",
                    BindingFlags.NonPublic | BindingFlags.Instance);
            var dict = _dictField?.GetValue(codeCollection) as IDictionary;
            if (dict == null || dict.Count == 0) return null;

            object codeObj = null;
            if (instanceId != null && dict.Contains(instanceId))
                codeObj = dict[instanceId];
            else
                foreach (DictionaryEntry e in dict) codeObj = e.Value;

            if (codeObj == null) return null;
            if (_codeTextField == null)
                _codeTextField = codeObj.GetType().GetField("_code",
                    BindingFlags.NonPublic | BindingFlags.Instance);
            return _codeTextField?.GetValue(codeObj) as string;
        }

        static void GenerateGroup(string groupId, GroupPreset preset, int count, string outputDir, string styleName)
        {
            Directory.CreateDirectory(outputDir);

            ImageStyle? fixedStyle = null;
            if (!string.IsNullOrEmpty(styleName) && Enum.TryParse<ImageStyle>(styleName, true, out var parsed))
                fixedStyle = parsed;

            Console.WriteLine($"[{groupId}] Generating {count} images ({preset.Width}x{preset.Height})...");
            int ok = 0, errors = 0;

            for (int i = 0; i < count; i++)
            {
                try
                {
                    var style = fixedStyle ?? AllStyles[Rng.Next(AllStyles.Length)];
                    int len = Rng.Next(preset.LabelMin, preset.LabelMax + 1);
                    string captchaId = "gen";
                    string instanceId = Guid.NewGuid().ToString("N");

                    if (i > 0 && i % 50 == 0)
                        SetupHttpContext();

                    var captcha = new CaptchaBase(captchaId);
                    captcha.CodeLength = len;
                    captcha.ImageStyle = style;
                    captcha.ImageSize = new BotDetect.ImageSize(preset.Width, preset.Height);
                    captcha.ImageFormat = BotDetect.ImageFormat.Png;

                    captcha.GetCode(instanceId, CodeGenerationPurpose.ImageGeneration);

                    _persistCodeCollectionMethod?.Invoke(null, new[] { captchaId, CcField.GetValue(captcha) });
                    _persistImageSizeMethod?.Invoke(null, new object[] { captchaId, captcha.ImageSize });
                    _persistImageFormatMethod?.Invoke(null, new object[] { captchaId, captcha.ImageFormat });

                    using (var imgStream = captcha.GetImage(instanceId))
                    {
                        if (imgStream == null || imgStream.Length == 0)
                        {
                            errors++;
                            continue;
                        }

                        string label = ReadActualCode(CcField.GetValue(captcha), instanceId);
                        if (string.IsNullOrEmpty(label))
                        {
                            errors++;
                            continue;
                        }

                        // BotDetect may output wrong size — resize to target dimensions
                        imgStream.Position = 0;
                        using (var srcBmp = new Bitmap(imgStream))
                        {
                            string filePath = Path.Combine(outputDir, $"{i:D6}_{label}.png");
                            if (srcBmp.Width == preset.Width && srcBmp.Height == preset.Height)
                            {
                                srcBmp.Save(filePath, System.Drawing.Imaging.ImageFormat.Png);
                            }
                            else
                            {
                                using (var dst = new Bitmap(preset.Width, preset.Height))
                                using (var g = Graphics.FromImage(dst))
                                {
                                    g.InterpolationMode = InterpolationMode.HighQualityBicubic;
                                    g.DrawImage(srcBmp, 0, 0, preset.Width, preset.Height);
                                    dst.Save(filePath, System.Drawing.Imaging.ImageFormat.Png);
                                }
                            }
                        }
                        ok++;
                    }

                    if ((i + 1) % 200 == 0)
                        Console.Write($"\r  [{groupId}] {i + 1}/{count} (ok={ok})");
                }
                catch (Exception ex)
                {
                    errors++;
                    if (errors <= 3)
                        Console.Error.WriteLine($"\n  Error [{i}]: {ex.GetType().Name}: {ex.Message}");
                    if (errors > count / 2 && errors > 10)
                    {
                        Console.Error.WriteLine($"\n  Too many errors ({errors}), stopping.");
                        break;
                    }
                }
            }

            Console.WriteLine($"\n[{groupId}] Done: {ok} images saved to {outputDir}" +
                (errors > 0 ? $" ({errors} errors)" : ""));
        }

        static void ListGroups()
        {
            Console.WriteLine("Available groups:");
            foreach (var kv in Groups.OrderBy(x => x.Key))
                Console.WriteLine($"  {kv.Key,-15} {kv.Value.Width,4}x{kv.Value.Height,-4}  " +
                    $"charset={kv.Value.Charset.Length,2}  len={kv.Value.LabelMin}-{kv.Value.LabelMax}");
            Console.WriteLine($"\nBotDetect image styles ({AllStyles.Length}):");
            for (int i = 0; i < AllStyles.Length; i += 4)
            {
                var line = string.Join("  ",
                    AllStyles.Skip(i).Take(4).Select(s => s.ToString().PadRight(20)));
                Console.WriteLine($"  {line}");
            }
        }

        static void PrintUsage()
        {
            Console.WriteLine(@"
BotDetect Captcha Generator
============================
Usage:
  BotDetectGen <group_id> [count] [output_dir] [style]
  BotDetectGen all [count] [output_dir] [style]
  BotDetectGen list

Arguments:
  group_id   - Group ID (e.g. g_250x40) or 'all'
  count      - Number of images to generate (default: 100)
  output_dir - Output directory (default: ../generated/<group>)
  style      - BotDetect ImageStyle name (default: random)

Examples:
  BotDetectGen g_250x40 1000
  BotDetectGen g_250x40 500 C:\data\captchas Wave
  BotDetectGen all 200
  BotDetectGen list
");
        }
    }

    class GroupPreset
    {
        public int Width { get; }
        public int Height { get; }
        public string Charset { get; }
        public int LabelMin { get; }
        public int LabelMax { get; }

        public GroupPreset(int w, int h, string charset, int labelMin, int labelMax)
        {
            Width = w; Height = h; Charset = charset;
            LabelMin = labelMin; LabelMax = labelMax;
        }
    }
}
