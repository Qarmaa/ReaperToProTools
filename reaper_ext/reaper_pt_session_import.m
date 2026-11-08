// reaper_pt_session_import.m — расширение Reaper: File → Import Pro Tools Session…
// Читает .ptx движком ptx_to_reaper.py (Python, Pro Tools не нужен), показывает сводку и предпроверку и вставляет дорожки прямо в
// ОТКРЫТЫЙ проект Reaper (через API Reaper, без сохранения нового .rpp). Собственное расширение, не связано с чужими.
#import <Cocoa/Cocoa.h>

#ifndef PX_VERSION
#define PX_VERSION "dev"
#endif

typedef struct { unsigned char fVirt; unsigned short key, cmd; } PX_ACCEL;
typedef struct { PX_ACCEL accel; const char *desc; } px_gaccel_register_t;
typedef struct {
    int caller_version;
    void *hwnd_main;
    int (*Register)(const char *name, void *infostruct);
    void *(*GetFunc)(const char *name);
} px_reaper_plugin_info_t;

typedef void *MediaTrack;
static int g_cmdId = 0;
static void (*f_InsertTrackAtIndex)(int, bool);
static MediaTrack (*f_GetTrack)(void *, int);
static int (*f_CountTracks)(void *);
static bool (*f_SetTrackStateChunk)(MediaTrack, const char *, bool);
static void *(*f_AddMediaItemToTrack)(MediaTrack);
static bool (*f_SetItemStateChunk)(void *, const char *, bool);
static bool (*f_GetTrackStateChunk)(MediaTrack, char *, int, bool);
static MediaTrack (*f_GetMasterTrack)(void *);
static bool (*f_SetMediaTrackInfo_Value)(MediaTrack, const char *, double);
static double (*f_GetMediaTrackInfo_Value)(MediaTrack, const char *);
static int (*f_TrackFX_GetCount)(MediaTrack);
static int (*f_TrackFX_AddByName)(MediaTrack, const char *, bool, int);
static bool (*f_TrackFX_GetNamedConfigParm)(MediaTrack, int, const char *, char *, int);
static bool (*f_TrackFX_SetNamedConfigParm)(MediaTrack, int, const char *, const char *);
static void (*f_TrackFX_Delete)(MediaTrack, int);
static void (*f_DeleteTrack)(MediaTrack);
static int (*f_TrackFX_GetNumParams)(MediaTrack, int);
static bool (*f_TrackFX_GetParamName)(MediaTrack, int, int, char *, int);
static bool (*f_TrackFX_SetParamNormalized)(MediaTrack, int, int, double);
static double (*f_TrackFX_GetParameterStepSizes)(MediaTrack, int, int, double *, double *, double *, bool *);
static int (*f_AddProjectMarker2)(void *, bool, double, double, const char *, int, int);
static void (*f_GetSetProjectNotes)(void *, bool, char *, int);
static void (*f_Undo_BeginBlock2)(void *);
static void (*f_Undo_EndBlock2)(void *, const char *, int);
static void (*f_UpdateArrange)(void);
static void (*f_PreventUIRefresh)(int);
static void (*f_TrackList_AdjustWindows)(bool);
static void (*f_GetProjectPath)(char *, int);
static double (*f_GetProjectTimeOffset)(void *, bool);
static double (*f_GetSetProjectInfo)(void *, const char *, double, bool);
static void (*f_Main_SaveProjectEx)(void *, const char *, int);
static unsigned int (*f_GetSetTrackGroupMembership)(MediaTrack, const char *, unsigned int, unsigned int);
static bool (*f_GetSetMediaTrackInfo_String)(MediaTrack, const char *, char *, bool);

static NSString *kPython = @"/usr/bin/python3";

static NSString *EngineDir(void) {
    NSFileManager *fm = [NSFileManager defaultManager];
    NSString *env = NSProcessInfo.processInfo.environment[@"REAPER_PTX_ENGINE"];
    NSArray *cands = @[
        env ?: @"",
        @"/Applications/Reaper to Pro Tools.app/Contents/Resources/ptx_tools",                                   // движок приложения — всегда свежий
        [NSHomeDirectory() stringByAppendingPathComponent:@"Applications/Reaper to Pro Tools.app/Contents/Resources/ptx_tools"],
        [NSHomeDirectory() stringByAppendingPathComponent:@"Library/Application Support/Reaper to Pro Tools/engine/ptx_tools"],   // запасной вариант без приложения
    ];
    for (NSString *c in cands)
        if (c.length && [fm fileExistsAtPath:[c stringByAppendingPathComponent:@"ptx_to_reaper.py"]]) return c;
    return nil;
}

// ------------------------------------------------------------------------------------------------ разбор .rpp и вставка

@interface PXProject : NSObject
@property (strong) NSMutableArray<NSString *> *tracks;         // тексты блоков <TRACK …>
@property (strong) NSMutableArray<NSString *> *headerLines;    // строки верхнего уровня (MASTER_VOLUME, MARKER …)
@property (strong) NSString *notesText;                        // содержимое <NOTES> без «|»
@property (strong) NSString *masterFX;                         // блок <MASTERFXLIST …> как <FXCHAIN …>
@end
@implementation PXProject
@end

static PXProject *ParseRPP(NSString *text) {
    PXProject *p = [PXProject new];
    p.tracks = [NSMutableArray array];
    p.headerLines = [NSMutableArray array];
    int depth = 0;
    NSMutableArray *block = nil;
    NSString *blockName = nil;
    for (NSString *line in [text componentsSeparatedByString:@"\n"]) {
        NSString *t = [line stringByTrimmingCharactersInSet:NSCharacterSet.whitespaceCharacterSet];
        if (depth == 0) { if ([t hasPrefix:@"<REAPER_PROJECT"]) depth = 1; continue; }
        if (depth == 1) {
            if ([t hasPrefix:@"<"]) {
                block = [NSMutableArray arrayWithObject:line];
                NSRange sp = [t rangeOfString:@" "];
                blockName = [t substringWithRange:NSMakeRange(1, (sp.location == NSNotFound ? t.length : sp.location) - 1)];
                depth = 2;
            } else if ([t isEqualToString:@">"]) break;
            else if (t.length) [p.headerLines addObject:t];
            continue;
        }
        [block addObject:line];
        if ([t hasPrefix:@"<"]) depth++;
        else if ([t isEqualToString:@">"]) {
            depth--;
            if (depth == 1) {
                NSString *joined = [block componentsJoinedByString:@"\n"];
                if ([blockName isEqualToString:@"TRACK"]) [p.tracks addObject:joined];
                else if ([blockName isEqualToString:@"MASTERFXLIST"]) {
                    NSMutableArray *b2 = [block mutableCopy];
                    b2[0] = @"<FXCHAIN";
                    p.masterFX = [b2 componentsJoinedByString:@"\n"];
                } else if ([blockName isEqualToString:@"NOTES"]) {
                    NSMutableArray *ls = [NSMutableArray array];
                    for (NSUInteger i = 1; i + 1 < block.count; i++) {
                        NSString *l = [block[i] stringByTrimmingCharactersInSet:NSCharacterSet.whitespaceCharacterSet];
                        if ([l hasPrefix:@"|"]) l = [l substringFromIndex:1];
                        [ls addObject:l];
                    }
                    p.notesText = [ls componentsJoinedByString:@"\n"];
                }
                block = nil;
            }
        }
    }
    return p;
}

// Заменяет все {GUID} на новые (одинаковые значения — на одинаковые), AUXRECV: номер дорожки-источника += base.
static NSString *PrepareChunk(NSString *chunk, int base, NSMutableDictionary *guidMap) {
    NSMutableString *out = [NSMutableString string];
    for (NSString *line in [chunk componentsSeparatedByString:@"\n"]) {
        NSString *t = [line stringByTrimmingCharactersInSet:NSCharacterSet.whitespaceCharacterSet];
        NSString *l = line;
        if ([t hasPrefix:@"AUXRECV "]) {
            NSRange r = [line rangeOfString:@"AUXRECV "];
            NSString *rest = [line substringFromIndex:r.location + r.length];
            NSScanner *sc = [NSScanner scannerWithString:rest];
            int src = 0;
            if ([sc scanInt:&src]) {
                l = [NSString stringWithFormat:@"%@AUXRECV %d%@", [line substringToIndex:r.location], src + base, [rest substringFromIndex:sc.scanLocation]];
            }
        }
        [out appendString:l];
        [out appendString:@"\n"];
    }
    NSRegularExpression *re = [NSRegularExpression regularExpressionWithPattern:@"\\{[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\\}" options:0 error:nil];
    NSArray *ms = [re matchesInString:out options:0 range:NSMakeRange(0, out.length)];
    for (NSTextCheckingResult *m in [ms reverseObjectEnumerator]) {
        NSString *g = [out substringWithRange:m.range];
        NSString *ng = guidMap[g];
        if (!ng) { ng = [NSString stringWithFormat:@"{%@}", NSUUID.UUID.UUIDString]; guidMap[g] = ng; }
        [out replaceCharactersInRange:m.range withString:ng];
    }
    return out;
}

// Вставляет дорожки, мастер, маркеры и заметки в открытый проект. Возвращает число вставленных дорожек (или -1) и текст сводки.
// Делит текст дорожки на «шапку» (без items) и список блоков <ITEM ...>: items вставляются по одному, чтобы интерфейс не замирал
static NSString *SplitItems(NSString *chunk, NSMutableArray<NSString *> *items) {
    NSMutableString *head = [NSMutableString string], *cur = nil;
    int depth = 0;
    for (NSString *line in [chunk componentsSeparatedByString:@"\n"]) {
        NSString *t = [line stringByTrimmingCharactersInSet:NSCharacterSet.whitespaceCharacterSet];
        BOOL open = [t hasPrefix:@"<"], close = [t isEqualToString:@">"];
        if (!cur && open && depth == 1 && [t hasPrefix:@"<ITEM"]) cur = [NSMutableString string];
        if (cur) [cur appendFormat:@"%@\n", line]; else [head appendFormat:@"%@\n", line];
        if (open) depth++; else if (close) depth--;
        if (cur && close && depth == 1) { [items addObject:cur]; cur = nil; }
    }
    if (cur) [head appendString:cur];                                     // незакрытый блок — оставляем как есть
    return head;
}


// ------------------------------------------------------------------ параметры плагинов по именам (средствами Reaper)
// Движок присылает [{track, fx, name, params: [[имя, значение, вид]]}]: имя параметра Pro Tools -> параметр плагина Reaper (любой формат: VST/VST3/AU/CLAP),
// значение: вид 'n' — нормализованное 0..1, 'i' — номер состояния (делится на число шагов параметра).
static NSString *SquashName(NSString *x) {
    NSMutableString *o = [NSMutableString string];
    for (NSUInteger i = 0; i < x.length; i++) { unichar c = [x characterAtIndex:i]; if ((c >= '0' && c <= '9') || (c >= 'a' && c <= 'z')) [o appendFormat:@"%C", c]; else if (c >= 'A' && c <= 'Z') [o appendFormat:@"%C", (unichar)(c + 32)]; }
    return o;
}

static NSString *TokenKey(NSString *x) {                         // «Split1LM» / «lm split1» -> «1 lm split»: слова без учёта порядка
    NSMutableArray *toks = [NSMutableArray array];
    NSMutableString *cur = [NSMutableString string];
    int prev = 0;                                                  // 1 — строчная, 2 — заглавная, 3 — цифра
    for (NSUInteger i = 0; i <= x.length; i++) {
        unichar c = i < x.length ? [x characterAtIndex:i] : ' ';
        int k = (c >= 'a' && c <= 'z') ? 1 : (c >= 'A' && c <= 'Z') ? 2 : (c >= '0' && c <= '9') ? 3 : 0;
        BOOL split = k == 0 || (k == 3) != (prev == 3) || (k == 2 && prev == 1);
        if (split && cur.length) { [toks addObject:cur.lowercaseString]; cur = [NSMutableString string]; }
        if (k) [cur appendFormat:@"%C", c];
        prev = k;
    }
    return [[toks sortedArrayUsingSelector:@selector(compare:)] componentsJoinedByString:@" "];
}

static void ApplyParamRequest(NSDictionary *req, int base) {
    if (!f_TrackFX_GetNumParams || !f_TrackFX_GetParamName || !f_TrackFX_SetParamNormalized) return;
    MediaTrack tr = f_GetTrack(NULL, base + [req[@"track"] intValue]);
    int fx = [req[@"fx"] intValue];
    if (!tr) return;
    int n = f_TrackFX_GetNumParams(tr, fx);
    NSMutableDictionary *byLow = [NSMutableDictionary dictionary], *bySq = [NSMutableDictionary dictionary], *byTok = [NSMutableDictionary dictionary];
    char buf[512];
    for (int i = 0; i < n; i++) {
        buf[0] = 0;
        if (!f_TrackFX_GetParamName(tr, fx, i, buf, sizeof buf)) continue;
        NSString *nm = [NSString stringWithUTF8String:buf] ?: @"";
        if (!byLow[nm.lowercaseString]) byLow[nm.lowercaseString] = @(i);
        if (!bySq[SquashName(nm)]) bySq[SquashName(nm)] = @(i);
        if (!byTok[TokenKey(nm)]) byTok[TokenKey(nm)] = @(i);
    }
    for (NSArray *p in req[@"params"]) {
        NSString *name = p[0]; double v = [p[1] doubleValue]; BOOL discrete = [p[2] isEqualToString:@"i"];
        int hint = p.count > 3 ? [p[3] intValue] : -1;                      // индекс, определённый движком по списку параметров плагина, надёжнее имени
        NSNumber *ix = (hint >= 0 && hint < n) ? @(hint) : (byLow[name.lowercaseString] ?: bySq[SquashName(name)] ?: byTok[TokenKey(name)]);
        if (!ix) continue;
        double norm = v;
        if (discrete) {
            double step = 0, small = 0, large = 0; bool toggle = false;
            if (f_TrackFX_GetParameterStepSizes && f_TrackFX_GetParameterStepSizes(tr, fx, ix.intValue, &step, &small, &large, &toggle) && !toggle && step > 0 && step <= 1) norm = v * step;
            else norm = v > 0.5 ? 1.0 : 0.0;
        }
        f_TrackFX_SetParamNormalized(tr, fx, ix.intValue, MAX(0.0, MIN(1.0, norm)));
    }
}

static NSString *g_timing = nil;                                  // только для автоматических проверок: время вставки
static void After(double sec, void (^b)(void));

// Вставляет дорожки, мастер, маркеры и заметки в открытый проект ПОРЦИЯМИ (по ~50 мс работы за такт), чтобы Reaper не «замирал»:
// между порциями главный поток обрабатывает события интерфейса. done получает число вставленных дорожек (или -1).
static void InsertIntoProjectAsync(PXProject *p, NSString *sessionName, NSMutableArray<NSString *> *notesOut, NSArray *paramReqs, void (^progress)(NSString *), void (^done)(int)) {
    if (!f_InsertTrackAtIndex || !f_SetTrackStateChunk || !f_GetTrack || !f_CountTracks) { done(-1); return; }
    if (f_Undo_BeginBlock2) f_Undo_BeginBlock2(NULL);
    CFAbsoluteTime tStart = CFAbsoluteTimeGetCurrent();
    __block double maxTick = 0;
    NSMutableString *slow = [NSMutableString string];
    int base = f_CountTracks(NULL);
    __block int n = 0;
    NSMutableDictionary *guidMap = [NSMutableDictionary dictionary];
    NSMutableDictionary<NSNumber *, NSString *> *withRecv = [NSMutableDictionary dictionary];
    NSArray<NSString *> *chunks = p.tracks;
    NSMutableArray<NSNumber *> *itemTrack = [NSMutableArray array];             // для каждого item — индекс его дорожки
    NSMutableArray<NSString *> *itemChunk = [NSMutableArray array];
    BOOL splitItems = f_AddMediaItemToTrack && f_SetItemStateChunk;
    __block NSUInteger ti = 0, ri = 0, ii = 0, qi = 0;
    __block NSArray<NSNumber *> *recvKeys = nil;
    __block int phase = 0;
    __block void (^step)(void) = nil;
    step = ^{
        CFAbsoluteTime t0 = CFAbsoluteTimeGetCurrent();
        BOOL finished = NO;
        if (f_PreventUIRefresh) f_PreventUIRefresh(1);
        if (phase == 0) {
            while (ti < chunks.count && CFAbsoluteTimeGetCurrent() - t0 < 0.05) {
                int idx = base + n;
                f_InsertTrackAtIndex(idx, false);
                MediaTrack tr = f_GetTrack(NULL, idx);
                if (!tr) { ti = chunks.count; break; }
                NSString *c = PrepareChunk(chunks[ti], base, guidMap);
                if (splitItems) {
                    NSMutableArray<NSString *> *its = [NSMutableArray array];
                    c = SplitItems(c, its);
                    for (NSString *it in its) { [itemTrack addObject:@(idx)]; [itemChunk addObject:it]; }
                }
                f_SetTrackStateChunk(tr, c.UTF8String, false);
                if ([c rangeOfString:@"AUXRECV "].location != NSNotFound) withRecv[@(idx)] = c;
                n++; ti++;
            }
            progress([NSString stringWithFormat:@"Inserting tracks %d/%lu…", n, (unsigned long)chunks.count]);
            if (ti >= chunks.count) { phase = 1; recvKeys = withRecv.allKeys; }
        } else if (phase == 1) {
            // sends (AUXRECV): Reaper отбрасывает приём, если дорожка-источник ещё не существует (приёмник стоит выше источников),
            // поэтому после вставки ВСЕХ дорожек применяем состояние приёмников повторно (до items: SetTrackStateChunk заменяет и items дорожки)
            while (ri < recvKeys.count && CFAbsoluteTimeGetCurrent() - t0 < 0.05) {
                MediaTrack tr = f_GetTrack(NULL, recvKeys[ri].intValue);
                if (tr) f_SetTrackStateChunk(tr, withRecv[recvKeys[ri]].UTF8String, false);
                ri++;
            }
            progress(@"Applying sends…");
            if (ri >= recvKeys.count) phase = 2;
        } else if (phase == 2) {
            while (ii < itemChunk.count && CFAbsoluteTimeGetCurrent() - t0 < 0.05) {
                MediaTrack tr = f_GetTrack(NULL, itemTrack[ii].intValue);
                void *it = tr ? f_AddMediaItemToTrack(tr) : NULL;
                if (it) f_SetItemStateChunk(it, itemChunk[ii].UTF8String, false);
                ii++;
            }
            progress([NSString stringWithFormat:@"Adding items %lu/%lu…", (unsigned long)ii, (unsigned long)itemChunk.count]);
            if (ii >= itemChunk.count) phase = 3;
        } else if (phase == 3) {
            while (qi < paramReqs.count && CFAbsoluteTimeGetCurrent() - t0 < 0.05) { ApplyParamRequest(paramReqs[qi], base); qi++; }
            if (paramReqs.count) progress([NSString stringWithFormat:@"Setting plug-in parameters %lu/%lu…", (unsigned long)qi, (unsigned long)paramReqs.count]);
            if (qi >= paramReqs.count) phase = 4;
        } else {
        // мастер: громкость / пан / mute / цвет — если в проекте они по умолчанию; FX — если у мастера проекта нет эффектов
        MediaTrack master = f_GetMasterTrack ? f_GetMasterTrack(NULL) : NULL;
        if (master && f_SetMediaTrackInfo_Value && f_GetMediaTrackInfo_Value) {
            for (NSString *h in p.headerLines) {
                if ([h hasPrefix:@"MASTER_VOLUME "]) {
                    NSArray *a = [[h substringFromIndex:14] componentsSeparatedByString:@" "];
                    if (a.count >= 2 && fabs(f_GetMediaTrackInfo_Value(master, "D_VOL") - 1.0) < 1e-9 && fabs(f_GetMediaTrackInfo_Value(master, "D_PAN")) < 1e-9) {
                        f_SetMediaTrackInfo_Value(master, "D_VOL", [a[0] doubleValue]);
                        f_SetMediaTrackInfo_Value(master, "D_PAN", [a[1] doubleValue]);
                    } else if (a.count >= 2 && ([a[0] doubleValue] != 1.0)) [notesOut addObject:@"Master volume/pan of the session was not applied (the project's master is already set)."];
                } else if ([h hasPrefix:@"MASTERMUTESOLO "] && [[h substringFromIndex:15] intValue]) f_SetMediaTrackInfo_Value(master, "B_MUTE", 1);
                else if ([h hasPrefix:@"MASTERPEAKCOL "]) f_SetMediaTrackInfo_Value(master, "I_CUSTOMCOLOR", [[h substringFromIndex:14] intValue]);
            }
            if (p.masterFX && f_GetTrackStateChunk && f_TrackFX_GetCount) {
                if (f_TrackFX_GetCount(master) == 0) {
                    char *buf = malloc(1 << 20);
                    if (buf && f_GetTrackStateChunk(master, buf, 1 << 20, false)) {
                        NSMutableString *mc = [NSMutableString stringWithUTF8String:buf];
                        NSRange last = [mc rangeOfString:@">" options:NSBackwardsSearch];
                        if (last.location != NSNotFound) {
                            [mc insertString:[PrepareChunk(p.masterFX, base, guidMap) stringByAppendingString:@""] atIndex:last.location];
                            f_SetTrackStateChunk(master, mc.UTF8String, false);
                        }
                    }
                    free(buf);
                } else [notesOut addObject:@"The project's master already has effects; the session's master effects were not added."];
            }
        }
        // маркеры и регионы: `MARKER idx pos "name" isregion ...`; у региона вторая строка с тем же idx и пустым именем — конец региона
        if (f_AddProjectMarker2) {
            NSMutableDictionary<NSNumber *, NSArray *> *regionStart = [NSMutableDictionary dictionary];
            for (NSString *h in p.headerLines) {
                if (![h hasPrefix:@"MARKER "]) continue;
                NSScanner *sc = [NSScanner scannerWithString:[h substringFromIndex:7]];
                int idx = 0, isRegion = 0; double pos = 0; NSString *name = @"";
                [sc scanInt:&idx]; [sc scanDouble:&pos];
                [sc scanCharactersFromSet:NSCharacterSet.whitespaceCharacterSet intoString:NULL];
                if ([sc scanString:@"\"" intoString:NULL]) { [sc scanUpToString:@"\"" intoString:&name]; [sc scanString:@"\"" intoString:NULL]; }
                else [sc scanUpToCharactersFromSet:NSCharacterSet.whitespaceCharacterSet intoString:&name];
                [sc scanInt:&isRegion];
                name = name ?: @"";
                if (isRegion) {
                    NSArray *st = regionStart[@(idx)];
                    if (st) { f_AddProjectMarker2(NULL, true, [st[0] doubleValue], pos, [st[1] UTF8String], -1, 0); [regionStart removeObjectForKey:@(idx)]; }
                    else regionStart[@(idx)] = @[@(pos), name];
                } else f_AddProjectMarker2(NULL, false, pos, 0, name.UTF8String, -1, 0);
            }
        }
        // заметки проекта (комментарии дорожек, непереносимые плагины)
        if (p.notesText.length && f_GetSetProjectNotes) {
            char *cur = calloc(1, 1 << 20);
            f_GetSetProjectNotes(NULL, false, cur, 1 << 20);
            NSString *curS = [NSString stringWithUTF8String:cur] ?: @"";
            NSString *all = [NSString stringWithFormat:@"%@%@---- Imported from Pro Tools session %@ ----\n%@\n", curS, curS.length ? @"\n\n" : @"", sessionName, p.notesText];
            f_GetSetProjectNotes(NULL, true, (char *)all.UTF8String, (int)strlen(all.UTF8String) + 1);
            free(cur);
        }
            finished = YES;
        }
        if (f_PreventUIRefresh) f_PreventUIRefresh(-1);
        if (f_TrackList_AdjustWindows) f_TrackList_AdjustWindows(false);
        if (f_UpdateArrange) f_UpdateArrange();
        double dt = CFAbsoluteTimeGetCurrent() - t0;
        maxTick = MAX(maxTick, dt);
        if (dt > 0.3) [slow appendFormat:@" [phase %d ti=%lu ri=%lu ii=%lu %.2fs]", phase, (unsigned long)ti, (unsigned long)ri, (unsigned long)ii, dt];
        if (finished) {
            if (f_Undo_EndBlock2) f_Undo_EndBlock2(NULL, "Import Pro Tools session", -1);
            g_timing = [NSString stringWithFormat:@"insert total %.2fs, longest UI freeze %.2fs; slow ticks:%@", CFAbsoluteTimeGetCurrent() - tStart, maxTick, slow];
            void (^fin)(int) = done;
            step = nil;
            fin(n);
            return;
        }
        After(0.004, step);
    };
    step();
}

// ------------------------------------------------------------------------------------------------------------------ UI

static const CGFloat kW = 336;
static const CGFloat kSide = 330;                                 // ширина панели списка дорожек                                   // ширина содержимого главного окна

static NSTextField *Label(NSString *s, BOOL bold, CGFloat size, NSColor *color) {
    NSTextField *t = [NSTextField labelWithString:s];
    t.font = bold ? [NSFont boldSystemFontOfSize:size] : [NSFont systemFontOfSize:size];
    if (color) t.textColor = color;
    t.lineBreakMode = NSLineBreakByWordWrapping;
    t.maximumNumberOfLines = 0;
    t.preferredMaxLayoutWidth = kW;
    return t;
}

static NSView *IconRow(NSString *symbol, NSColor *tint, NSString *text, NSView *trailing) {
    NSImageSymbolConfiguration *cfg = [NSImageSymbolConfiguration configurationWithPointSize:13 weight:NSFontWeightRegular];
    NSImage *img = [[NSImage imageWithSystemSymbolName:symbol accessibilityDescription:nil] imageWithSymbolConfiguration:cfg];
    NSImageView *iv = [NSImageView imageViewWithImage:img ?: [NSImage new]];
    iv.contentTintColor = tint;
    [iv setContentHuggingPriority:NSLayoutPriorityRequired forOrientation:NSLayoutConstraintOrientationHorizontal];
    NSTextField *t = Label(text, NO, 11, nil);
    t.preferredMaxLayoutWidth = trailing ? 230 : kW - 26;
    NSMutableArray *views = [NSMutableArray arrayWithObjects:iv, t, nil];
    if (trailing) [views addObject:trailing];
    NSStackView *r = [NSStackView stackViewWithViews:views];
    r.spacing = 6;
    r.alignment = NSLayoutAttributeCenterY;
    return r;
}

static void After(double sec, void (^b)(void)) {                 // таймер в common-режимах: работает и внутри модального окна
    NSTimer *t = [NSTimer timerWithTimeInterval:sec repeats:NO block:^(NSTimer *x) { b(); }];
    [[NSRunLoop mainRunLoop] addTimer:t forMode:NSRunLoopCommonModes];
}

static NSString *Num(NSNumber *n) { return n ? n.stringValue : @"0"; }

static NSStackView *VStack(CGFloat spacing) {
    NSStackView *s = [NSStackView stackViewWithViews:@[]];
    s.orientation = NSUserInterfaceLayoutOrientationVertical;
    s.alignment = NSLayoutAttributeLeading;
    s.spacing = spacing;
    return s;
}

static NSString *RelinkJSONFile(NSDictionary *map) {
    if (!map.count) return nil;
    NSData *d = [NSJSONSerialization dataWithJSONObject:map options:0 error:nil];
    NSString *f = [NSTemporaryDirectory() stringByAppendingPathComponent:[NSString stringWithFormat:@"ptx_relink_%@.json", NSUUID.UUID.UUIDString]];
    [d writeToFile:f atomically:YES];
    return f;
}

@class PXImporter;
typedef NSTask *(^PXRunner)(NSArray *args, void (^done)(int code, NSString *out));

// ------------------------------------------------------------------------------------------------ окно Relink (как в приложении)

@interface PXRelink : NSObject <NSTableViewDataSource, NSTableViewDelegate>
@property (strong) NSWindow *win;
@property (strong) NSArray<NSDictionary *> *missing;
@property (strong) NSMutableArray<NSString *> *folders;
@property (strong) NSMutableDictionary<NSString *, NSArray *> *candidates;
@property (strong) NSMutableDictionary<NSString *, NSString *> *chosen;
@property (strong) NSString *selected;
@property (strong) NSTableView *filesTable, *candTable;
@property (strong) NSTextField *count, *folderText, *candHint, *errorLabel;
@property (strong) NSButton *findBtn, *commitBtn, *matchFormat, *matchDuration;
@property (strong) NSButton *radioExact, *radioLoose;
@property (strong) NSProgressIndicator *spin;
@property (strong) NSStackView *folderList;
@property (copy) PXRunner runner;
@property (strong) NSString *ptxPath;
@property (strong) NSDictionary *existing;
@property (assign) BOOL searched, committed;
- (instancetype)initWithMissing:(NSArray *)missing existing:(NSDictionary *)existing ptx:(NSString *)ptx runner:(PXRunner)runner;
- (NSDictionary *)runModalResult;
@end

@implementation PXRelink

- (instancetype)initWithMissing:(NSArray *)missing existing:(NSDictionary *)existing ptx:(NSString *)ptx runner:(PXRunner)runner {
    if (!(self = [super init])) return nil;
    self.missing = missing;
    self.existing = existing;
    self.ptxPath = ptx;
    self.runner = runner;
    self.folders = [NSMutableArray array];
    self.candidates = [NSMutableDictionary dictionary];
    self.chosen = [NSMutableDictionary dictionary];
    self.selected = missing.firstObject[@"path"];
    [self build];
    return self;
}

- (NSScrollView *)scrollWith:(NSTableView *)tv {
    NSScrollView *sv = [NSScrollView new];
    sv.documentView = tv;
    sv.hasVerticalScroller = YES;
    sv.drawsBackground = NO;
    sv.borderType = NSNoBorder;
    return sv;
}

- (NSTableView *)makeTable {
    NSTableView *tv = [NSTableView new];
    NSTableColumn *c = [[NSTableColumn alloc] initWithIdentifier:@"c"];
    c.resizingMask = NSTableColumnAutoresizingMask;
    [tv addTableColumn:c];
    tv.headerView = nil;
    tv.dataSource = self;
    tv.delegate = self;
    tv.columnAutoresizingStyle = NSTableViewUniformColumnAutoresizingStyle;
    tv.gridStyleMask = NSTableViewSolidHorizontalGridLineMask;
    tv.backgroundColor = NSColor.clearColor;
    return tv;
}

- (void)build {
    NSWindow *w = [[NSWindow alloc] initWithContentRect:NSMakeRect(0, 0, 880, 600) styleMask:NSWindowStyleMaskTitled backing:NSBackingStoreBuffered defer:NO];
    w.title = @"Relink";
    w.releasedWhenClosed = NO;
    NSView *root = w.contentView;

    NSTextField *title = Label(@"Relink", YES, 22, nil);
    self.count = Label(@"", NO, 13, NSColor.secondaryLabelColor);
    self.count.alignment = NSTextAlignmentRight;
    NSBox *line1 = [NSBox new]; line1.boxType = NSBoxSeparator;
    NSBox *vline = [NSBox new]; vline.boxType = NSBoxSeparator;
    NSBox *line2 = [NSBox new]; line2.boxType = NSBoxSeparator;

    // левая колонка
    NSTextField *lh = Label(@"Files to Relink", YES, 15, nil);
    self.filesTable = [self makeTable];
    self.filesTable.rowHeight = 30;
    NSScrollView *lsv = [self scrollWith:self.filesTable];

    // правая колонка
    NSTextField *oh = Label(@"Linking Options", YES, 15, nil);
    NSTextField *mb = Label(@"Match by", NO, 13, nil);
    self.radioExact = [NSButton radioButtonWithTitle:@"Find By Name" target:self action:@selector(optionsChanged:)];
    self.radioLoose = [NSButton radioButtonWithTitle:@"Find By Name (ignore case and symbols)" target:self action:@selector(optionsChanged:)];
    self.radioExact.state = NSControlStateValueOn;
    NSStackView *radios = VStack(4);
    [radios addArrangedSubview:self.radioExact]; [radios addArrangedSubview:self.radioLoose];
    NSStackView *modeRow = [NSStackView stackViewWithViews:@[mb, radios]];
    modeRow.alignment = NSLayoutAttributeTop; modeRow.spacing = 14;
    self.matchFormat = [NSButton checkboxWithTitle:@"Match Format (sample rate of the session)" target:self action:@selector(optionsChanged:)];
    self.matchFormat.state = NSControlStateValueOn;
    self.matchDuration = [NSButton checkboxWithTitle:@"Match Duration (long enough for the session)" target:self action:@selector(optionsChanged:)];
    self.matchDuration.state = NSControlStateValueOn;
    NSBox *sep1 = [NSBox new]; sep1.boxType = NSBoxSeparator;

    NSTextField *ah = Label(@"Areas to Search", YES, 15, nil);
    NSButton *add = [NSButton buttonWithTitle:@"Add Folder…" target:self action:@selector(addFolder:)];
    self.findBtn = [NSButton buttonWithTitle:@"Find Candidates" target:self action:@selector(find:)];
    self.findBtn.enabled = NO;
    NSView *sp = [NSView new]; [sp setContentHuggingPriority:NSLayoutPriorityDefaultLow forOrientation:NSLayoutConstraintOrientationHorizontal];
    NSStackView *areaHead = [NSStackView stackViewWithViews:@[ah, sp, add, self.findBtn]];
    areaHead.spacing = 8;
    self.folderList = VStack(3);
    self.folderText = Label(@"No folder selected. Add a folder to search for missing files.", NO, 12, NSColor.secondaryLabelColor);
    self.folderText.preferredMaxLayoutWidth = 480;
    self.spin = [NSProgressIndicator new];
    self.spin.style = NSProgressIndicatorStyleSpinning; self.spin.controlSize = NSControlSizeSmall; self.spin.displayedWhenStopped = NO;
    self.errorLabel = Label(@"", NO, 11, NSColor.systemRedColor);
    self.errorLabel.hidden = YES;
    self.spin.hidden = YES;
    NSBox *sep2 = [NSBox new]; sep2.boxType = NSBoxSeparator;
    NSTextField *ch = Label(@"Candidates", YES, 15, nil);
    self.candHint = Label(@"", NO, 12, NSColor.secondaryLabelColor);
    self.candHint.preferredMaxLayoutWidth = 480;
    self.candTable = [self makeTable];
    self.candTable.rowHeight = 40;
    NSScrollView *csv = [self scrollWith:self.candTable];

    NSStackView *right = VStack(12);
    for (NSView *v in @[oh, modeRow, self.matchFormat, self.matchDuration, sep1, areaHead, self.folderList, self.folderText, self.spin, self.errorLabel, sep2, ch, self.candHint]) [right addArrangedSubview:v];
    sep1.translatesAutoresizingMaskIntoConstraints = NO; sep2.translatesAutoresizingMaskIntoConstraints = NO;
    areaHead.translatesAutoresizingMaskIntoConstraints = NO;
    [sep1.widthAnchor constraintEqualToConstant:500].active = YES;
    [sep2.widthAnchor constraintEqualToConstant:500].active = YES;
    [areaHead.widthAnchor constraintEqualToConstant:500].active = YES;

    NSButton *cancel = [NSButton buttonWithTitle:@"Cancel" target:self action:@selector(cancel:)];
    cancel.keyEquivalent = @"\033";
    self.commitBtn = [NSButton buttonWithTitle:@"Commit Links" target:self action:@selector(commit:)];
    self.commitBtn.keyEquivalent = @"\r";
    self.commitBtn.enabled = NO;
    NSView *sp2 = [NSView new]; [sp2 setContentHuggingPriority:NSLayoutPriorityDefaultLow forOrientation:NSLayoutConstraintOrientationHorizontal];
    NSStackView *bottom = [NSStackView stackViewWithViews:@[sp2, cancel, self.commitBtn]];
    bottom.spacing = 10;

    NSArray *all = @[title, self.count, line1, lh, lsv, vline, right, csv, line2, bottom];
    for (NSView *v in all) { v.translatesAutoresizingMaskIntoConstraints = NO; [root addSubview:v]; }
    NSDictionary *views = NSDictionaryOfVariableBindings(title, _count, line1, lh, lsv, vline, right, csv, line2, bottom);
    (void)views;
    NSLayoutConstraint *(^eq)(NSLayoutAnchor *, NSLayoutAnchor *, CGFloat) = ^NSLayoutConstraint *(NSLayoutAnchor *a, NSLayoutAnchor *b, CGFloat c) {
        NSLayoutConstraint *k = [a constraintEqualToAnchor:b constant:c]; k.active = YES; return k;
    };
    eq(title.leadingAnchor, root.leadingAnchor, 18); eq(title.topAnchor, root.topAnchor, 14);
    eq(self.count.trailingAnchor, root.trailingAnchor, -18); eq(self.count.centerYAnchor, title.centerYAnchor, 0);
    eq(line1.topAnchor, root.topAnchor, 52); eq(line1.leadingAnchor, root.leadingAnchor, 0); eq(line1.trailingAnchor, root.trailingAnchor, 0);
    eq(line2.bottomAnchor, root.bottomAnchor, -56); eq(line2.leadingAnchor, root.leadingAnchor, 0); eq(line2.trailingAnchor, root.trailingAnchor, 0);
    eq(vline.leadingAnchor, root.leadingAnchor, 330); eq(vline.topAnchor, line1.bottomAnchor, 0); eq(vline.bottomAnchor, line2.topAnchor, 0);
    [vline.widthAnchor constraintEqualToConstant:1].active = YES;
    eq(lh.leadingAnchor, root.leadingAnchor, 16); eq(lh.topAnchor, line1.bottomAnchor, 14);
    eq(lsv.leadingAnchor, root.leadingAnchor, 8); eq(lsv.trailingAnchor, vline.leadingAnchor, -8);
    eq(lsv.topAnchor, lh.bottomAnchor, 8); eq(lsv.bottomAnchor, line2.topAnchor, -8);
    eq(right.leadingAnchor, vline.trailingAnchor, 16); eq(right.topAnchor, line1.bottomAnchor, 14);
    eq(csv.leadingAnchor, vline.trailingAnchor, 8); eq(csv.trailingAnchor, root.trailingAnchor, -8);
    eq(csv.topAnchor, right.bottomAnchor, 6); eq(csv.bottomAnchor, line2.topAnchor, -8);
    eq(bottom.trailingAnchor, root.trailingAnchor, -18); eq(bottom.bottomAnchor, root.bottomAnchor, -16);
    self.win = w;
    [self refresh];
}

- (void)refresh {
    self.count.stringValue = [NSString stringWithFormat:@"%lu missing · %lu linked", (unsigned long)self.missing.count, (unsigned long)self.chosen.count];
    self.commitBtn.enabled = self.chosen.count > 0;
    self.findBtn.enabled = self.folders.count > 0 && self.missing.count > 0;
    for (NSView *v in [self.folderList.arrangedSubviews copy]) { [self.folderList removeArrangedSubview:v]; [v removeFromSuperview]; }
    for (NSString *f in self.folders) {
        NSTextField *t = Label(f, NO, 12, nil);
        t.lineBreakMode = NSLineBreakByTruncatingHead; t.maximumNumberOfLines = 1; t.preferredMaxLayoutWidth = 480;
        NSImageView *iv = [NSImageView imageViewWithImage:[NSImage imageWithSystemSymbolName:@"folder" accessibilityDescription:nil]];
        iv.contentTintColor = NSColor.secondaryLabelColor;
        NSButton *x = [NSButton buttonWithImage:[NSImage imageWithSystemSymbolName:@"xmark.circle" accessibilityDescription:nil] target:self action:@selector(removeFolder:)];
        x.bordered = NO; x.identifier = f; x.contentTintColor = NSColor.secondaryLabelColor;
        NSStackView *r = [NSStackView stackViewWithViews:@[iv, t, x]]; r.spacing = 6;
        [self.folderList addArrangedSubview:r];
    }
    self.folderText.stringValue = self.folders.count ? @"Selected folders are searched recursively, including all subfolders." : @"No folder selected. Add a folder to search for missing files.";
    self.folderText.font = [NSFont systemFontOfSize:self.folders.count ? 11 : 12];
    NSDictionary *sel = nil;
    for (NSDictionary *m in self.missing) if ([m[@"path"] isEqualToString:self.selected]) sel = m;
    NSArray *vis = [self visible:self.selected];
    if (!sel) self.candHint.stringValue = @"Select a missing file to see candidates.";
    else if (!self.searched) self.candHint.stringValue = @"Add a folder and press Find Candidates.";
    else if (!vis.count) self.candHint.stringValue = [NSString stringWithFormat:@"No candidates for %@ with the current options.", sel[@"name"]];
    else self.candHint.stringValue = @"";
    [self.filesTable reloadData];
    [self.candTable reloadData];
}

- (NSArray *)visible:(NSString *)path {
    if (!path) return @[];
    BOOL loose = self.radioLoose.state == NSControlStateValueOn, fmt = self.matchFormat.state == NSControlStateValueOn, dur = self.matchDuration.state == NSControlStateValueOn;
    NSMutableArray *out = [NSMutableArray array];
    for (NSDictionary *c in self.candidates[path]) {
        if ((loose || [c[@"exact"] boolValue]) && (!fmt || [c[@"format_ok"] boolValue]) && (!dur || [c[@"duration_ok"] boolValue])) [out addObject:c];
    }
    return out;
}

- (void)optionsChanged:(id)sender { [self refresh]; }

- (void)addFolder:(id)sender {
    NSOpenPanel *op = [NSOpenPanel openPanel];
    op.canChooseFiles = NO; op.canChooseDirectories = YES; op.allowsMultipleSelection = YES;
    op.message = @"Choose folders to search for the missing files (searched recursively)";
    if ([op runModal] != NSModalResponseOK) return;
    for (NSURL *u in op.URLs) if (![self.folders containsObject:u.path]) [self.folders addObject:u.path];
    [self refresh];
}

- (void)removeFolder:(NSButton *)b { [self.folders removeObject:b.identifier]; [self refresh]; }

- (void)find:(id)sender {
    self.findBtn.enabled = NO;
    self.spin.hidden = NO;
    [self.spin startAnimation:nil];
    self.errorLabel.hidden = YES;
    NSMutableArray *args = [NSMutableArray arrayWithObjects:self.ptxPath, @"--candidates", nil];
    for (NSString *f in self.folders) { [args addObject:@"--search"]; [args addObject:f]; }
    NSString *rf = RelinkJSONFile(self.existing);
    if (rf) { [args addObject:@"--relink"]; [args addObject:rf]; }
    self.runner(args, ^(int code, NSString *out) {
        [self.spin stopAnimation:nil];
        self.spin.hidden = YES;
        NSDictionary *j = nil;
        for (NSString *ln in [out componentsSeparatedByString:@"\n"]) if ([ln hasPrefix:@"{"]) j = [NSJSONSerialization JSONObjectWithData:[ln dataUsingEncoding:NSUTF8StringEncoding] options:0 error:nil];
        self.searched = YES;
        if (!j) { self.errorLabel.stringValue = out ?: @"Search failed."; self.errorLabel.hidden = NO; }
        else {
            [self.candidates removeAllObjects];
            [self.candidates addEntriesFromDictionary:j[@"candidates"]];
            for (NSDictionary *m in self.missing) {                          // единственный подходящий — привязываем сразу
                NSString *p = m[@"path"];
                if (self.chosen[p]) continue;
                NSArray *v = [self visible:p];
                if (v.count == 1) self.chosen[p] = v[0][@"path"];
            }
        }
        [self refresh];
    });
}

- (void)cancel:(id)sender { self.committed = NO; [NSApp stopModal]; [self.win orderOut:nil]; }
- (void)commit:(id)sender { self.committed = YES; [NSApp stopModal]; [self.win orderOut:nil]; }

- (NSDictionary *)runModalResult {
    [self.win center];
    NSString *snap = NSProcessInfo.processInfo.environment[@"REAPER_PTX_TEST_RELINK_SNAP"];            // только для автоматических проверок
    if (snap.length) {
        NSString *dir = NSProcessInfo.processInfo.environment[@"REAPER_PTX_TEST_RELINK_DIR"];
        if (dir.length) { self.radioLoose.state = NSControlStateValueOn; [self.folders addObject:dir]; [self refresh]; After(0.5, ^{ [self find:nil]; }); }
        After(3, ^{
            NSView *v = self.win.contentView.superview;
            NSBitmapImageRep *rep = [v bitmapImageRepForCachingDisplayInRect:v.bounds];
            [v cacheDisplayInRect:v.bounds toBitmapImageRep:rep];
            [[rep representationUsingType:NSBitmapImageFileTypePNG properties:@{}] writeToFile:snap atomically:YES];
            [self commit:nil];
        });
    }
    [NSApp runModalForWindow:self.win];
    return self.committed ? [self.chosen copy] : nil;
}

// --- таблицы

- (NSInteger)numberOfRowsInTableView:(NSTableView *)tv {
    return tv == self.filesTable ? (NSInteger)self.missing.count : (NSInteger)[self visible:self.selected].count;
}

- (NSView *)tableView:(NSTableView *)tv viewForTableColumn:(NSTableColumn *)col row:(NSInteger)row {
    NSTableCellView *cell = [NSTableCellView new];
    if (tv == self.filesTable) {
        NSDictionary *m = self.missing[row];
        BOOL on = self.chosen[m[@"path"]] != nil;
        NSImageView *iv = [NSImageView imageViewWithImage:[NSImage imageWithSystemSymbolName:on ? @"checkmark.circle.fill" : @"circle.dashed" accessibilityDescription:nil]];
        iv.contentTintColor = on ? NSColor.systemGreenColor : NSColor.secondaryLabelColor;
        NSTextField *t = Label(m[@"name"], NO, 13, nil);
        t.lineBreakMode = NSLineBreakByTruncatingMiddle; t.maximumNumberOfLines = 1;
        t.preferredMaxLayoutWidth = 250;
        [t setContentCompressionResistancePriority:NSLayoutPriorityDefaultLow forOrientation:NSLayoutConstraintOrientationHorizontal];
        [iv setContentCompressionResistancePriority:NSLayoutPriorityRequired forOrientation:NSLayoutConstraintOrientationHorizontal];
        [iv setContentHuggingPriority:NSLayoutPriorityRequired forOrientation:NSLayoutConstraintOrientationHorizontal];
        NSStackView *r = [NSStackView stackViewWithViews:@[iv, t]]; r.spacing = 8;
        r.translatesAutoresizingMaskIntoConstraints = NO;
        [cell addSubview:r];
        [r.leadingAnchor constraintEqualToAnchor:cell.leadingAnchor constant:6].active = YES;
        [r.trailingAnchor constraintEqualToAnchor:cell.trailingAnchor constant:-6].active = YES;
        [r.centerYAnchor constraintEqualToAnchor:cell.centerYAnchor].active = YES;
        return cell;
    }
    NSDictionary *c = [self visible:self.selected][row];
    BOOL on = [self.chosen[self.selected] isEqualToString:c[@"path"]];
    NSImageView *iv = [NSImageView imageViewWithImage:[NSImage imageWithSystemSymbolName:on ? @"largecircle.fill.circle" : @"circle" accessibilityDescription:nil]];
    iv.contentTintColor = on ? NSColor.controlAccentColor : NSColor.secondaryLabelColor;
    NSString *path = c[@"path"];
    NSTextField *n = Label(path.lastPathComponent, NO, 13, nil);
    n.maximumNumberOfLines = 1; n.lineBreakMode = NSLineBreakByTruncatingMiddle;
    NSTextField *d = Label(path.stringByDeletingLastPathComponent, NO, 11, NSColor.secondaryLabelColor);
    d.maximumNumberOfLines = 1; d.lineBreakMode = NSLineBreakByTruncatingHead;
    NSStackView *two = VStack(1); [two addArrangedSubview:n]; [two addArrangedSubview:d];
    NSTextField *info = Label([NSString stringWithFormat:@"%.1f s · %d-bit · %d ch · %d Hz", [c[@"seconds"] doubleValue], [c[@"bits"] intValue], [c[@"channels"] intValue], [c[@"rate"] intValue]], NO, 11, NSColor.secondaryLabelColor);
    info.maximumNumberOfLines = 1;
    [info setContentCompressionResistancePriority:NSLayoutPriorityRequired forOrientation:NSLayoutConstraintOrientationHorizontal];
    [iv setContentCompressionResistancePriority:NSLayoutPriorityRequired forOrientation:NSLayoutConstraintOrientationHorizontal];
    [iv setContentHuggingPriority:NSLayoutPriorityRequired forOrientation:NSLayoutConstraintOrientationHorizontal];
    n.preferredMaxLayoutWidth = 300; d.preferredMaxLayoutWidth = 300;
    [two setContentCompressionResistancePriority:NSLayoutPriorityDefaultLow forOrientation:NSLayoutConstraintOrientationHorizontal];
    NSStackView *r = [NSStackView stackViewWithViews:@[iv, two, info]]; r.spacing = 8;
    r.translatesAutoresizingMaskIntoConstraints = NO;
    [cell addSubview:r];
    [r.leadingAnchor constraintEqualToAnchor:cell.leadingAnchor constant:6].active = YES;
    [r.trailingAnchor constraintEqualToAnchor:cell.trailingAnchor constant:-6].active = YES;
    [r.centerYAnchor constraintEqualToAnchor:cell.centerYAnchor].active = YES;
    return cell;
}

- (void)tableViewSelectionDidChange:(NSNotification *)n {
    NSTableView *tv = n.object;
    NSInteger row = tv.selectedRow;
    if (row < 0) return;
    if (tv == self.filesTable) { self.selected = self.missing[row][@"path"]; [self refresh]; }
    else {
        NSString *p = [self visible:self.selected][row][@"path"];
        if ([self.chosen[self.selected] isEqualToString:p]) [self.chosen removeObjectForKey:self.selected]; else self.chosen[self.selected] = p;
        [tv deselectAll:nil];
        [self refresh];
    }
}

@end


// ---------------------------------------------------------------------- блоки FX, снятые у самого Reaper (вместо библиотеки)
// Reaper сам создаёт плагин по имени и пишет настоящий блок <VST ...> с обёрткой; движок использует его как запись библиотеки.
static NSString *ExtractVSTBlock(const char *chunk) {
    NSString *t = [NSString stringWithUTF8String:chunk ?: ""];
    NSRange r = [t rangeOfString:@"<VST \""];
    if (r.location == NSNotFound) return nil;
    NSMutableArray *out = [NSMutableArray array];
    for (NSString *l in [[t substringFromIndex:r.location] componentsSeparatedByString:@"\n"]) {
        NSString *x = [l stringByTrimmingCharactersInSet:NSCharacterSet.whitespaceCharacterSet];
        [out addObject:x];
        if ([x isEqualToString:@">"]) break;
    }
    return [out componentsJoinedByString:@"\n"];
}

static void HarvestFX(NSArray<NSString *> *names, void (^progress)(NSString *), void (^done)(NSString *)) {
    if (!names.count || !f_TrackFX_AddByName || !f_InsertTrackAtIndex || !f_GetTrackStateChunk || !f_DeleteTrack || !f_TrackFX_Delete) { done(nil); return; }
    int n0 = f_CountTracks(NULL);
    f_InsertTrackAtIndex(n0, false);
    MediaTrack tr = f_GetTrack(NULL, n0);
    if (!tr) { done(nil); return; }
    const int kBuf = 1 << 24;
    char *buf = malloc(kBuf);
    NSMutableString *lib = [NSMutableString string];
    __block NSUInteger i = 0;
    __block void (^step)(void) = nil;
    step = ^{
        CFAbsoluteTime t0 = CFAbsoluteTimeGetCurrent();
        if (f_PreventUIRefresh) f_PreventUIRefresh(1);
        while (i < names.count && CFAbsoluteTimeGetCurrent() - t0 < 0.05) {
            int fx = f_TrackFX_AddByName(tr, names[i].UTF8String, false, -1);
            if (fx >= 0) {
                buf[0] = 0;
                if (f_GetTrackStateChunk(tr, buf, kBuf, false)) {
                    NSString *blk = ExtractVSTBlock(buf);
                    if (blk.length) [lib appendFormat:@"#PLUGIN\t%@\n%@\n#END\n", names[i], blk];
                }
                f_TrackFX_Delete(tr, fx);
            }
            i++;
        }
        progress([NSString stringWithFormat:@"Preparing plug-ins %lu/%lu…", (unsigned long)MIN(i, names.count), (unsigned long)names.count]);
        if (f_PreventUIRefresh) f_PreventUIRefresh(-1);
        if (i >= names.count) {
            f_DeleteTrack(tr);
            free(buf);
            if (f_TrackList_AdjustWindows) f_TrackList_AdjustWindows(false);
            NSString *path = nil;
            if (lib.length) {
                path = [NSTemporaryDirectory() stringByAppendingPathComponent:[NSString stringWithFormat:@"px_fx_library_%@.txt", NSUUID.UUID.UUIDString]];
                [lib writeToFile:path atomically:YES encoding:NSUTF8StringEncoding error:nil];
            }
            void (^fin)(NSString *) = done;
            step = nil;
            fin(path);
            return;
        }
        After(0.004, step);
    };
    step();
}

// ------------------------------------------------------------------------------------------ выбор дорожек для импорта

@interface PXTrackPicker : NSObject <NSTableViewDataSource, NSTableViewDelegate>
@property (strong) NSView *view;                                  // панель: заголовок, поиск и фильтр, «All / None», таблица
@property (strong) NSArray<NSDictionary *> *tracks;               // все дорожки сессии
@property (strong) NSArray<NSDictionary *> *visible;              // дорожки, прошедшие фильтр (их показывает таблица)
@property (strong) NSMutableSet<NSNumber *> *selected;
@property (strong) NSTableView *table;
@property (strong) NSTextField *count;
@property (strong) NSSearchField *search;
@property (strong) NSButton *filterButton;
@property (strong) NSMutableDictionary<NSString *, NSNumber *> *show;      // пункты меню фильтра: что показывать (как в Pro Tools)
@property (copy) void (^onChange)(void);
- (void)applyFilter;
- (void)setTracks:(NSArray *)tracks;                              // список дорожек сессии (все выбраны, если набор не совпадает с прежним)
- (NSSet *)selectedOrNil;                                         // nil — выбраны все
@end

@implementation PXTrackPicker

- (instancetype)init {
    if (!(self = [super init])) return nil;
    self.tracks = @[];
    self.visible = @[];
    self.selected = [NSMutableSet set];
    self.show = [NSMutableDictionary dictionary];
    for (NSString *k in @[@"inactive", @"hidden", @"frozen", @"noclips", @"noauto", @"nomarkers", @"folder", @"audio", @"aux", @"vca", @"video", @"other"]) self.show[k] = @YES;
    NSView *root = [NSView new];
    NSTextField *title = Label(@"Tracks", YES, 13, nil);
    self.table = [NSTableView new];
    NSTableColumn *c = [[NSTableColumn alloc] initWithIdentifier:@"c"];
    c.resizingMask = NSTableColumnAutoresizingMask;
    [self.table addTableColumn:c];
    self.table.headerView = nil;
    self.table.dataSource = self;
    self.table.delegate = self;
    self.table.rowHeight = 24;
    self.table.columnAutoresizingStyle = NSTableViewUniformColumnAutoresizingStyle;
    self.table.selectionHighlightStyle = NSTableViewSelectionHighlightStyleNone;
    self.table.backgroundColor = NSColor.clearColor;
    NSScrollView *sv = [NSScrollView new];
    sv.documentView = self.table;
    sv.hasVerticalScroller = YES;
    sv.borderType = NSNoBorder;
    sv.drawsBackground = NO;
    NSButton *all = [NSButton buttonWithTitle:@"All" target:self action:@selector(selectAll:)];
    NSButton *none = [NSButton buttonWithTitle:@"None" target:self action:@selector(selectNone:)];
    for (NSButton *b in @[all, none]) { b.bezelStyle = NSBezelStyleSmallSquare; b.controlSize = NSControlSizeSmall; b.font = [NSFont systemFontOfSize:11]; b.refusesFirstResponder = YES; }
    self.count = [NSTextField labelWithString:@""];
    self.count.textColor = NSColor.secondaryLabelColor;
    self.count.font = [NSFont systemFontOfSize:11];
    NSView *flex = [NSView new];
    [flex setContentHuggingPriority:NSLayoutPriorityDefaultLow forOrientation:NSLayoutConstraintOrientationHorizontal];
    NSStackView *top = [NSStackView stackViewWithViews:@[title, flex, self.count, all, none]];
    top.spacing = 8;
    top.alignment = NSLayoutAttributeCenterY;
    // поиск по имени + меню фильтра (как в Pro Tools: Inactive / без клипов / без автоматизации / без маркеров / типы дорожек)
    self.search = [NSSearchField new];
    self.search.placeholderString = @"Filter";
    self.search.controlSize = NSControlSizeSmall;
    self.search.font = [NSFont systemFontOfSize:12];
    self.search.target = self;
    self.search.action = @selector(filterChanged:);
    self.search.sendsSearchStringImmediately = YES;
    self.filterButton = [NSButton buttonWithImage:[NSImage imageWithSystemSymbolName:@"line.3.horizontal.decrease.circle" accessibilityDescription:@"Filter"] ?: [NSImage new] target:self action:@selector(showFilterMenu:)];
    self.filterButton.bezelStyle = NSBezelStyleTexturedRounded;
    self.filterButton.controlSize = NSControlSizeSmall;
    self.filterButton.toolTip = @"Show tracks by state and type";
    self.filterButton.refusesFirstResponder = YES;
    NSStackView *fbar = [NSStackView stackViewWithViews:@[self.search, self.filterButton]];
    fbar.spacing = 6;
    fbar.alignment = NSLayoutAttributeCenterY;
    for (NSView *v in @[top, fbar, sv]) { v.translatesAutoresizingMaskIntoConstraints = NO; [root addSubview:v]; }
    [NSLayoutConstraint activateConstraints:@[
        [top.topAnchor constraintEqualToAnchor:root.topAnchor constant:14], [top.leadingAnchor constraintEqualToAnchor:root.leadingAnchor constant:16], [top.trailingAnchor constraintEqualToAnchor:root.trailingAnchor constant:-16],
        [fbar.topAnchor constraintEqualToAnchor:top.bottomAnchor constant:8], [fbar.leadingAnchor constraintEqualToAnchor:root.leadingAnchor constant:16], [fbar.trailingAnchor constraintEqualToAnchor:root.trailingAnchor constant:-16],
        [sv.topAnchor constraintEqualToAnchor:fbar.bottomAnchor constant:8], [sv.leadingAnchor constraintEqualToAnchor:root.leadingAnchor constant:8], [sv.trailingAnchor constraintEqualToAnchor:root.trailingAnchor constant:-8],
        [sv.bottomAnchor constraintEqualToAnchor:root.bottomAnchor constant:-12],
    ]];
    self.view = root;
    [self refreshCount];
    return self;
}

// группа типа дорожки для меню фильтра
static NSString *KindGroup(NSString *kind) {
    if ([kind isEqualToString:@"folder"] || [kind isEqualToString:@"routing"]) return @"folder";
    if ([kind isEqualToString:@"audio"]) return @"audio";
    if ([kind isEqualToString:@"aux"]) return @"aux";
    if ([kind isEqualToString:@"VCA"]) return @"vca";
    if ([kind isEqualToString:@"video"]) return @"video";
    return @"other";
}

- (BOOL)passes:(NSDictionary *)t {
    NSString *q = self.search.stringValue;
    if (q.length && [t[@"name"] rangeOfString:q options:NSCaseInsensitiveSearch].location == NSNotFound) return NO;
    if (![self.show[KindGroup(t[@"kind"])] boolValue]) return NO;
    if ([t[@"inactive"] boolValue] && ![self.show[@"inactive"] boolValue]) return NO;
    if ([t[@"hidden"] boolValue] && ![self.show[@"hidden"] boolValue]) return NO;
    if ([t[@"frozen"] boolValue] && ![self.show[@"frozen"] boolValue]) return NO;
    if ([t[@"clips"] intValue] == 0 && ![self.show[@"noclips"] boolValue]) return NO;
    if (![t[@"automation"] boolValue] && ![self.show[@"noauto"] boolValue]) return NO;
    if ([t[@"markers"] intValue] == 0 && ![self.show[@"nomarkers"] boolValue]) return NO;
    return YES;
}

- (void)applyFilter {
    NSMutableArray *v = [NSMutableArray array];
    for (NSDictionary *t in self.tracks) if ([self passes:t]) [v addObject:t];
    self.visible = v;
    BOOL active = self.search.stringValue.length > 0;
    for (NSNumber *x in self.show.allValues) if (!x.boolValue) active = YES;
    self.filterButton.contentTintColor = active ? NSColor.controlAccentColor : nil;
    [self.table reloadData];
    [self refreshCount];
}

- (void)filterChanged:(id)sender { [self applyFilter]; }

- (void)showFilterMenu:(NSButton *)b {
    NSMenu *m = [NSMenu new];
    m.autoenablesItems = NO;
    NSArray *rows = @[@[@"inactive", @"Inactive Tracks"], @[@"hidden", @"Hidden Tracks"], @[@"frozen", @"Frozen Tracks"], @[@"noclips", @"Tracks without Clips"], @[@"noauto", @"Tracks without Automation"], @[@"nomarkers", @"Tracks without Markers"], @"-",
                      @[@"folder", @"Folder Tracks"], @[@"audio", @"Audio Tracks"], @[@"aux", @"Aux Inputs"], @[@"vca", @"VCA Masters"], @[@"video", @"Video Tracks"], @[@"other", @"Other Tracks"], @"-"];
    for (id r in rows) {
        if ([r isKindOfClass:[NSString class]]) { [m addItem:[NSMenuItem separatorItem]]; continue; }
        NSMenuItem *it = [[NSMenuItem alloc] initWithTitle:r[1] action:@selector(filterItemToggled:) keyEquivalent:@""];
        it.target = self; it.representedObject = r[0];
        it.state = [self.show[r[0]] boolValue] ? NSControlStateValueOn : NSControlStateValueOff;
        [m addItem:it];
    }
    NSMenuItem *all = [[NSMenuItem alloc] initWithTitle:@"Show All" action:@selector(filterShowAll:) keyEquivalent:@""];
    all.target = self;
    [m addItem:all];
    [m popUpMenuPositioningItem:nil atLocation:NSMakePoint(0, b.bounds.size.height + 2) inView:b];
}

- (void)filterItemToggled:(NSMenuItem *)it { NSString *k = it.representedObject; self.show[k] = @(![self.show[k] boolValue]); [self applyFilter]; }
- (void)filterShowAll:(id)sender { for (NSString *k in self.show.allKeys) self.show[k] = @YES; self.search.stringValue = @""; [self applyFilter]; }

- (void)setTracks:(NSArray *)tracks {
    NSSet *old = [NSSet setWithArray:[self.tracks valueForKey:@"index"]];
    NSSet *now = [NSSet setWithArray:[tracks valueForKey:@"index"]];
    _tracks = tracks;
    if (![old isEqualToSet:now]) { [self.selected removeAllObjects]; for (NSDictionary *t in tracks) [self.selected addObject:t[@"index"]]; }
    [self applyFilter];
}

- (NSSet *)selectedOrNil { return self.selected.count == self.tracks.count ? nil : [self.selected copy]; }
- (void)refreshCount {
    if (!self.tracks.count) self.count.stringValue = @"";
    else if (self.visible.count != self.tracks.count) self.count.stringValue = [NSString stringWithFormat:@"%lu of %lu · %lu shown", (unsigned long)self.selected.count, (unsigned long)self.tracks.count, (unsigned long)self.visible.count];
    else self.count.stringValue = [NSString stringWithFormat:@"%lu of %lu", (unsigned long)self.selected.count, (unsigned long)self.tracks.count];
    if (self.onChange) self.onChange();
}
// All / None действуют на показанные (отфильтрованные) дорожки
- (void)selectAll:(id)sender { for (NSDictionary *t in self.visible) [self.selected addObject:t[@"index"]]; [self.table reloadData]; [self refreshCount]; }
- (void)selectNone:(id)sender { for (NSDictionary *t in self.visible) [self.selected removeObject:t[@"index"]]; [self.table reloadData]; [self refreshCount]; }

- (NSInteger)numberOfRowsInTableView:(NSTableView *)tv { return (NSInteger)self.visible.count; }

- (NSView *)tableView:(NSTableView *)tv viewForTableColumn:(NSTableColumn *)col row:(NSInteger)row {
    NSDictionary *t = self.visible[row];
    NSTableCellView *cell = [NSTableCellView new];
    NSButton *cb = [NSButton checkboxWithTitle:t[@"name"] target:self action:@selector(toggled:)];
    cb.tag = row;
    cb.state = [self.selected containsObject:t[@"index"]] ? NSControlStateValueOn : NSControlStateValueOff;
    cb.lineBreakMode = NSLineBreakByTruncatingTail;
    cb.font = [NSFont systemFontOfSize:12];
    NSString *kind = t[@"kind"];
    int clips = [t[@"clips"] intValue];
    NSString *info = clips ? [NSString stringWithFormat:@"%@ · %d", kind, clips] : kind;
    NSTextField *lab = [NSTextField labelWithString:info];
    lab.textColor = NSColor.secondaryLabelColor;
    lab.font = [NSFont systemFontOfSize:10];
    lab.alignment = NSTextAlignmentRight;
    [lab setContentCompressionResistancePriority:NSLayoutPriorityRequired forOrientation:NSLayoutConstraintOrientationHorizontal];
    BOOL folder = [kind isEqualToString:@"folder"] || [kind isEqualToString:@"routing"];
    if (folder) cb.font = [NSFont boldSystemFontOfSize:12];
    BOOL filtered = self.search.stringValue.length > 0;                 // при поиске отступы по глубине не нужны
    cb.translatesAutoresizingMaskIntoConstraints = NO;
    lab.translatesAutoresizingMaskIntoConstraints = NO;
    [cell addSubview:cb]; [cell addSubview:lab];
    [NSLayoutConstraint activateConstraints:@[
        [cb.leadingAnchor constraintEqualToAnchor:cell.leadingAnchor constant:6 + (filtered ? 0 : 16 * [t[@"depth"] intValue])],
        [cb.centerYAnchor constraintEqualToAnchor:cell.centerYAnchor],
        [lab.trailingAnchor constraintEqualToAnchor:cell.trailingAnchor constant:-8], [lab.centerYAnchor constraintEqualToAnchor:cell.centerYAnchor],
        [cb.trailingAnchor constraintLessThanOrEqualToAnchor:lab.leadingAnchor constant:-6],
    ]];
    return cell;
}

- (void)toggled:(NSButton *)b {
    NSDictionary *t = self.visible[b.tag];
    BOOL on = b.state == NSControlStateValueOn;
    NSString *kind = t[@"kind"];
    NSMutableArray *affected = [NSMutableArray arrayWithObject:t[@"index"]];
    if ([kind isEqualToString:@"folder"] || [kind isEqualToString:@"routing"]) {                // папка включает/выключает всех своих детей (по полному списку)
        NSUInteger pos = [self.tracks indexOfObject:t];
        int d = [t[@"depth"] intValue];
        for (NSUInteger k = pos + 1; pos != NSNotFound && k < self.tracks.count && [self.tracks[k][@"depth"] intValue] > d; k++) [affected addObject:self.tracks[k][@"index"]];
    }
    for (NSNumber *i in affected) { if (on) [self.selected addObject:i]; else [self.selected removeObject:i]; }
    [self.table reloadData];
    [self refreshCount];
}

@end

// ------------------------------------------------------------------------------------------------------------ главное окно

// Пункты Track Data: {ключ, название, включён по умолчанию, поддерживается}. Группы разделяются линиями.
static NSArray<NSArray<NSArray *> *> *TrackDataGroups(void) {
    return @[
        @[@[@"clips", @"Clips and Media", @YES, @YES], @[@"clip_gain", @"Clip Gain", @YES, @YES], @[@"clip_mute", @"Clip Mute", @YES, @YES], @[@"fades", @"Fade Settings", @YES, @YES]],
        @[@[@"auto_vol", @"Main Volume Automation", @YES, @YES], @[@"auto_pan", @"Main Pan Automation (up to 7.1.2)", @YES, @YES], @[@"auto_mute", @"Main Mute Automation", @YES, @YES],
          @[@"main_routing", @"Main Routing Assignments", @YES, @YES]],
        @[@[@"send_auto_vol", @"Send Volume Automation", @YES, @YES], @[@"send_auto_pan", @"Send Pan Automation", @YES, @YES], @[@"send_auto_mute", @"Send Mute Automation", @YES, @YES],
          @[@"send_routing", @"Send Routing Assignments", @YES, @YES]],
        @[@[@"clip_colors", @"Clip Colors", @YES, @YES], @[@"track_colors", @"Track Colors", @YES, @YES], @[@"notes", @"Track Notes", @YES, @YES], @[@"solo", @"Solo State", @YES, @YES],
          @[@"solo_safe", @"Solo Safe", @NO, @NO], @[@"playlists", @"Playlists", @YES, @YES], @[@"markers", @"Memory Locations (Global Markers)", @YES, @YES],
          @[@"tempo", @"Tempo Map and Time Signature", @NO, @NO]],
        @[@[@"skip_hidden", @"Skip Hidden Tracks", @NO, @YES], @[@"skip_inactive", @"Skip Inactive Tracks", @NO, @YES]],
        @[@[@"trim_overlap", @"Trim Overlapping Clips", @NO, @YES]],
        @[@[@"mute", @"Mute State", @YES, @YES], @[@"fader", @"Track Volume / Pan", @YES, @YES], @[@"plugins", @"Plug-ins (as FX chain)", @YES, @YES],
          @[@"aux", @"Aux / Instrument as empty tracks", @YES, @YES], @[@"video", @"Video track (video item)", @YES, @YES], @[@"groups", @"Clip groups (as grouped / empty items)", @YES, @YES], @[@"vca", @"VCA masters and their groups (as Reaper VCA)", @YES, @YES]],
    ];
}

// ------------------------------------------------------------------------------------------ смещение по времени

typedef NS_ENUM(NSInteger, PXOffsetFormat) { PXOffsetTimecode = 0, PXOffsetMinSec = 1, PXOffsetSeconds = 2 };

// Разбирает смещение: «-1:30.5» (мин:сек), «-00:01:00:12» (таймкод hh:mm:ss:ff), «-90.5» (секунды); знак в начале (+/-). Пустая строка = 0.
static double ParseOffset(NSString *str, NSInteger fmt, double fps, BOOL *ok) {
    NSString *t = [[str stringByTrimmingCharactersInSet:NSCharacterSet.whitespaceCharacterSet] stringByReplacingOccurrencesOfString:@"," withString:@"."];
    *ok = YES;
    if (!t.length) return 0;
    double sign = 1;
    if ([t hasPrefix:@"-"]) { sign = -1; t = [t substringFromIndex:1]; } else if ([t hasPrefix:@"+"]) t = [t substringFromIndex:1];
    t = [t stringByTrimmingCharactersInSet:NSCharacterSet.whitespaceCharacterSet];
    if (!t.length) { *ok = NO; return 0; }
    NSMutableArray<NSString *> *parts = [[t componentsSeparatedByCharactersInSet:[NSCharacterSet characterSetWithCharactersInString:@":;"]] mutableCopy];
    for (NSString *p in parts) {
        NSScanner *sc = [NSScanner scannerWithString:p]; double d;
        if (![sc scanDouble:&d] || !sc.isAtEnd || d < 0) { *ok = NO; return 0; }
    }
    double v = 0;
    if (fmt == PXOffsetTimecode && parts.count >= 2) {                           // [[hh:]mm:]ss:ff
        double nom = fps > 1 ? round(fps) : 30;                                 // счёт кадров по номинальной частоте (29.97 → 30 кадров в «секунде» таймкода)
        double ff = [parts.lastObject doubleValue];
        [parts removeLastObject];
        for (NSString *p in parts) v = v * 60 + [p doubleValue];
        v = (v * nom + ff) / (fps > 1 ? fps : 30);
    } else {                                                                     // [[h:]m:]s[.fff] или секунды
        for (NSString *p in parts) v = v * 60 + [p doubleValue];
    }
    return sign * v;
}

static NSString *FormatOffset(double sec, NSInteger fmt, double fps) {
    double a = fabs(sec);
    NSString *sg = sec < 0 ? @"-" : @"";
    if (fmt == PXOffsetSeconds) { NSString *t = [NSString stringWithFormat:@"%.3f", a]; while ([t hasSuffix:@"0"]) t = [t substringToIndex:t.length - 1]; if ([t hasSuffix:@"."]) t = [t substringToIndex:t.length - 1]; return [sg stringByAppendingString:t]; }
    if (fmt == PXOffsetMinSec) {
        long total = lround(a * 1000), m = total / 60000, sec = (total / 1000) % 60, ms = total % 1000;
        return [NSString stringWithFormat:@"%@%03ld:%02ld.%03ld", sg, MIN(m, 999L), sec, ms];
    }
    long nom = fps > 1 ? lround(fps) : 30;
    long frames = lround(a * (fps > 1 ? fps : 30));
    long total = frames / nom, ff = frames % nom;
    return [NSString stringWithFormat:@"%@%02ld:%02ld:%02ld:%02ld", sg, total / 3600, (total / 60) % 60, total % 60, ff];
}

// Жёсткая маска поля смещения: из введённого берутся только знак и цифры, остальное отбрасывается.
//   Timecode: ±hh:mm:ss:ff — цифры вводятся справа налево (набрали 10 → 00:00:00:10), как в Pro Tools; mm и ss ≤ 59, ff меньше частоты кадров
//   Min:Sec:  ±mmm:ss.mmm  — так же справа налево; Seconds: ±число с точкой (до 3 знаков после неё)
static NSString *MaskOffsetText(NSString *raw, NSInteger fmt, double fps) {
    if (!raw.length) return @"";
    BOOL neg = NO;
    for (NSUInteger k = 0; k < raw.length; k++) { unichar c = [raw characterAtIndex:k]; if (c == '-') neg = YES; else if (c == '+') neg = NO; }
    NSString *sg = neg ? @"-" : @"";
    NSMutableString *digits = [NSMutableString string];
    NSMutableString *num = [NSMutableString string];
    BOOL dot = NO;
    for (NSUInteger k = 0; k < raw.length; k++) {
        unichar c = [raw characterAtIndex:k];
        if (c >= '0' && c <= '9') { [digits appendFormat:@"%C", c]; [num appendFormat:@"%C", c]; }
        else if ((c == '.' || c == ',') && !dot && fmt == PXOffsetSeconds) { dot = YES; [num appendString:@"."]; }
    }
    if (fmt == PXOffsetSeconds) {
        NSArray *p = [num componentsSeparatedByString:@"."];
        NSString *ip = p[0]; if (ip.length > 7) ip = [ip substringFromIndex:ip.length - 7];
        if (!ip.length && p.count > 1) ip = @"0";
        NSString *fp = p.count > 1 ? ([p[1] length] > 3 ? [p[1] substringToIndex:3] : p[1]) : nil;
        if (!ip.length && !fp) return sg;
        return [NSString stringWithFormat:@"%@%@%@", sg, ip, fp ? [@"." stringByAppendingString:fp] : @""];
    }
    if (!digits.length && !sg.length) return @"";
    NSUInteger n = fmt == PXOffsetTimecode ? 8 : 8;
    if (digits.length > n) [digits setString:[digits substringFromIndex:digits.length - n]];
    while (digits.length < n) [digits insertString:@"0" atIndex:0];
    if (fmt == PXOffsetTimecode) {
        int hh = [[digits substringWithRange:NSMakeRange(0, 2)] intValue], mm = MIN(59, [[digits substringWithRange:NSMakeRange(2, 2)] intValue]);
        int ss = MIN(59, [[digits substringWithRange:NSMakeRange(4, 2)] intValue]);
        int nom = fps > 1 ? (int)lround(fps) : 30;
        int ff = MIN(nom - 1, [[digits substringWithRange:NSMakeRange(6, 2)] intValue]);
        return [NSString stringWithFormat:@"%@%02d:%02d:%02d:%02d", sg, hh, mm, ss, ff];
    }
    int mins = [[digits substringWithRange:NSMakeRange(0, 3)] intValue], ss = MIN(59, [[digits substringWithRange:NSMakeRange(3, 2)] intValue]), ms = [[digits substringWithRange:NSMakeRange(5, 3)] intValue];
    return [NSString stringWithFormat:@"%@%03d:%02d.%03d", sg, mins, ss, ms];
}

@interface PXImporter : NSObject <NSWindowDelegate, NSTextFieldDelegate>
@property (strong) NSWindow *win, *optSheet;
@property (strong) NSString *ptxPath, *engine;
@property (strong) NSStackView *content, *inputBlock, *preflightBlock;
@property (strong) NSTextField *outputNote, *statusLabel;
@property (strong) NSSegmentedControl *mode;
@property (strong) NSButton *importButton, *cancelButton, *dataButton;
@property (strong) NSProgressIndicator *spin;
@property (strong) NSTask *task;
@property (strong) NSDictionary *info;
@property (strong) NSMutableDictionary<NSString *, NSString *> *relinkMap;
@property (strong) PXRelink *relinkWin;
@property (strong) NSMutableDictionary<NSString *, NSNumber *> *opts;
@property (strong) NSButton *mediaCopyBtn, *linkBtn;                    // «Copy from source media» / «Link to source media» (взаимоисключающие)
@property (strong) NSTextField *offsetField;                      // смещение всех времён
@property (strong) NSPopUpButton *offsetFormat;
@property (strong) NSTextField *offsetNote;
@property (strong) PXTrackPicker *picker;                         // список дорожек справа от основного окна
@property (strong) NSView *rootView;
- (void)start;
@end

static PXImporter *g_importer = nil;

@implementation PXImporter

- (void)start {
    self.engine = EngineDir();
    if (!self.engine) {
        NSAlert *a = [NSAlert new];
        a.messageText = @"Pro Tools session import: converter engine not found";
        a.informativeText = @"Install “Reaper to Pro Tools.app” into /Applications (it contains the converter), or copy its ptx_tools folder to ~/Library/Application Support/Reaper to Pro Tools/engine/.";
        [a runModal];
        return;
    }
    self.relinkMap = [NSMutableDictionary dictionary];
    self.opts = [NSMutableDictionary dictionary];
    for (NSArray *g in TrackDataGroups()) for (NSArray *it in g) self.opts[it[0]] = @([it[2] boolValue]);
    self.opts[@"lanes"] = @YES;
    NSString *fxTest = NSProcessInfo.processInfo.environment[@"REAPER_PTX_TEST_FXCHUNK"];       // только для автоматических проверок: что отдаёт Reaper для нового FX
    if (fxTest.length && f_InsertTrackAtIndex && f_TrackFX_AddByName) {
        After(4, ^{
            int n = f_CountTracks(NULL); f_InsertTrackAtIndex(n, false); MediaTrack tr = f_GetTrack(NULL, n);
            NSMutableString *out = [NSMutableString string];
            int fx = -1;
            for (NSString *cand in [fxTest componentsSeparatedByString:@"|"]) {                                      // несколько вариантов имени через «|»
                int r = f_TrackFX_AddByName(tr, cand.UTF8String, false, -1);
                [out appendFormat:@"TRY \"%@\" -> %d\n", cand, r];
                if (r >= 0 && fx < 0) fx = r;
            }
            [out appendFormat:@"FXADD idx=%d count=%d\n", fx, f_TrackFX_GetCount(tr)];
            for (NSString *key in @[@"vst_chunk", @"vst_chunk_program", @"fx_type", @"fx_name", @"fx_ident"]) {
                char *buf = calloc(1, 1 << 22); BOOL ok = f_TrackFX_GetNamedConfigParm(tr, fx, key.UTF8String, buf, 1 << 22);
                [out appendFormat:@"%@ ok=%d len=%lu head=%.80s\n", key, ok, (unsigned long)strlen(buf), buf]; free(buf);
            }
            char *tb = calloc(1, 1 << 22); f_GetTrackStateChunk(tr, tb, 1 << 22, false);
            [out appendFormat:@"TRACKCHUNK\n%s\n", tb]; free(tb);
            [out writeToFile:NSProcessInfo.processInfo.environment[@"REAPER_PTX_TEST_FXLOG"] atomically:YES encoding:NSUTF8StringEncoding error:nil];
        });
    }
    NSString *testPtx = NSProcessInfo.processInfo.environment[@"REAPER_PTX_TEST_PTX"];      // только для автоматических проверок
    if (testPtx.length) {
        self.ptxPath = testPtx;
    } else {
        NSOpenPanel *op = [NSOpenPanel openPanel];
        op.title = @"Choose a Pro Tools session";
        op.allowedFileTypes = @[@"ptx"];
        op.canChooseDirectories = NO;
        if ([op runModal] != NSModalResponseOK) return;
        self.ptxPath = op.URL.path;
    }
    [self buildWindow];
    [self loadInfo];
}

- (void)spinOn { self.spin.hidden = NO; [self.spin startAnimation:nil]; }
- (void)spinOff { [self.spin stopAnimation:nil]; self.spin.hidden = YES; }

- (void)buildWindow {
    NSStackView *st = VStack(0);
    st.edgeInsets = NSEdgeInsetsMake(14, 24, 18, 24);
    self.content = st;

    NSTextField *ver = Label([NSString stringWithFormat:@"v%s", PX_VERSION], NO, 10, NSColor.tertiaryLabelColor);
    NSTextField *inH = Label(@"Input", YES, 13, nil);
    self.inputBlock = VStack(5);
    [self.inputBlock addArrangedSubview:Label(@"Reading…", NO, 12, nil)];
    NSTextField *preH = Label(@"Preflight", YES, 13, nil);
    self.preflightBlock = VStack(6);
    NSTextField *outH = Label(@"Media options", YES, 13, nil);
    self.mode = [NSSegmentedControl segmentedControlWithLabels:@[@"Link", @"Standalone"] trackingMode:NSSegmentSwitchTrackingSelectOne target:self action:@selector(modeChanged:)];
    self.mode.selectedSegment = 1;                                   // по умолчанию — копировать аудио (Copy from source media); контрол — только модель, на экране две галочки
    self.mode.segmentStyle = NSSegmentStyleRounded;
    self.mode.translatesAutoresizingMaskIntoConstraints = NO;
    [self.mode.widthAnchor constraintEqualToConstant:kW].active = YES;
    self.mode.segmentDistribution = NSSegmentDistributionFillEqually;
    self.mode.refusesFirstResponder = YES;
    self.mediaCopyBtn = [NSButton checkboxWithTitle:@"Copy from source media" target:self action:@selector(modeChecked:)];
    self.linkBtn = [NSButton checkboxWithTitle:@"Link to source media" target:self action:@selector(modeChecked:)];
    for (NSButton *b in @[self.mediaCopyBtn, self.linkBtn]) b.refusesFirstResponder = YES;
    NSStackView *modeBox = VStack(4);
    [modeBox addArrangedSubview:self.mediaCopyBtn]; [modeBox addArrangedSubview:self.linkBtn];
    [self syncModeButtons];
    self.outputNote = Label(@"", NO, 11, NSColor.secondaryLabelColor);
    NSTextField *offH = Label(@"Time offset", YES, 13, nil);
    self.offsetField = [NSTextField textFieldWithString:@""];
    self.offsetField.placeholderString = @"00:00:00:00";
    self.offsetField.controlSize = NSControlSizeSmall;
    self.offsetField.font = [NSFont monospacedDigitSystemFontOfSize:12 weight:NSFontWeightRegular];
    self.offsetField.target = self;
    self.offsetField.action = @selector(offsetEdited:);
    self.offsetField.delegate = self;
    self.offsetField.translatesAutoresizingMaskIntoConstraints = NO;
    [self.offsetField.widthAnchor constraintEqualToConstant:150].active = YES;
    self.offsetFormat = [NSPopUpButton new];
    [self.offsetFormat addItemsWithTitles:@[@"Timecode", @"Min:Sec", @"Seconds"]];
    self.offsetFormat.controlSize = NSControlSizeSmall;
    self.offsetFormat.target = self;
    self.offsetFormat.action = @selector(offsetFormatChanged:);
    NSButton *zeroBtn = [NSButton buttonWithTitle:@"Reset" target:self action:@selector(offsetReset:)];
    zeroBtn.bezelStyle = NSBezelStyleSmallSquare; zeroBtn.controlSize = NSControlSizeSmall; zeroBtn.font = [NSFont systemFontOfSize:11]; zeroBtn.refusesFirstResponder = YES;
    NSStackView *offRow = [NSStackView stackViewWithViews:@[self.offsetField, self.offsetFormat, zeroBtn]];
    offRow.spacing = 8; offRow.alignment = NSLayoutAttributeCenterY;
    self.offsetNote = Label(@"Shifts everything (clips, automation, markers) in time. Type digits; start with - for an earlier time.", NO, 11, NSColor.secondaryLabelColor);
    self.statusLabel = Label(@"", NO, 11, NSColor.secondaryLabelColor);

    self.spin = [NSProgressIndicator new];
    self.spin.style = NSProgressIndicatorStyleSpinning;
    self.spin.controlSize = NSControlSizeSmall;
    self.spin.displayedWhenStopped = NO;
    self.spin.hidden = YES;
    self.dataButton = [NSButton buttonWithTitle:@"Track Data" target:self action:@selector(showTrackData:)];
    self.dataButton.bezelStyle = NSBezelStyleSmallSquare;
    self.dataButton.refusesFirstResponder = YES;
    self.cancelButton = [NSButton buttonWithTitle:@"Cancel" target:self action:@selector(cancel:)];
    self.importButton = [NSButton buttonWithTitle:@"Import" target:self action:@selector(doImport:)];
    self.importButton.keyEquivalent = @"\r";
    self.importButton.enabled = NO;
    for (NSButton *b in @[self.cancelButton, self.importButton]) { b.translatesAutoresizingMaskIntoConstraints = NO; [b.widthAnchor constraintEqualToConstant:88].active = YES; }
    self.dataButton.translatesAutoresizingMaskIntoConstraints = NO;
    [self.dataButton.widthAnchor constraintEqualToConstant:100].active = YES;
    NSView *spacer = [NSView new];
    [spacer setContentHuggingPriority:NSLayoutPriorityDefaultLow forOrientation:NSLayoutConstraintOrientationHorizontal];
    NSStackView *buttons = [NSStackView stackViewWithViews:@[self.dataButton, spacer, self.spin, self.cancelButton, self.importButton]];
    buttons.spacing = 14;
    buttons.translatesAutoresizingMaskIntoConstraints = NO;
    [buttons.widthAnchor constraintEqualToConstant:kW].active = YES;

    for (NSView *v in @[ver, inH, self.inputBlock, preH, self.preflightBlock, outH, modeBox, self.outputNote, offH, offRow, self.offsetNote, self.statusLabel, buttons]) [st addArrangedSubview:v];
    [st setCustomSpacing:14 afterView:ver];
    [st setCustomSpacing:6 afterView:inH];
    [st setCustomSpacing:26 afterView:self.inputBlock];
    [st setCustomSpacing:6 afterView:preH];
    [st setCustomSpacing:30 afterView:self.preflightBlock];
    [st setCustomSpacing:8 afterView:outH];
    [st setCustomSpacing:10 afterView:modeBox];
    [st setCustomSpacing:14 afterView:self.outputNote];
    [st setCustomSpacing:6 afterView:offH];
    [st setCustomSpacing:4 afterView:offRow];
    [st setCustomSpacing:6 afterView:self.offsetNote];
    [st setCustomSpacing:14 afterView:self.statusLabel];

    NSWindow *w = [[NSWindow alloc] initWithContentRect:NSMakeRect(0, 0, kW + 48, 520)
                                              styleMask:NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
                                                backing:NSBackingStoreBuffered defer:NO];
    w.title = @"Import PTX Session Data";
    w.releasedWhenClosed = NO;
    w.delegate = self;
    self.picker = [PXTrackPicker new];
    __weak PXImporter *ws = self;
    self.picker.onChange = ^{ PXImporter *si = ws; if (si && si.info && !si.task.isRunning) si.importButton.enabled = !si.picker.tracks.count || si.picker.selected.count > 0; };
    NSView *root = [NSView new];
    NSBox *sep = [NSBox new]; sep.boxType = NSBoxSeparator;
    for (NSView *v in @[st, sep, self.picker.view]) { v.translatesAutoresizingMaskIntoConstraints = NO; [root addSubview:v]; }
    [NSLayoutConstraint activateConstraints:@[
        [st.leadingAnchor constraintEqualToAnchor:root.leadingAnchor], [st.topAnchor constraintEqualToAnchor:root.topAnchor], [st.widthAnchor constraintEqualToConstant:kW + 48],
        [sep.leadingAnchor constraintEqualToAnchor:st.trailingAnchor], [sep.topAnchor constraintEqualToAnchor:root.topAnchor], [sep.bottomAnchor constraintEqualToAnchor:root.bottomAnchor], [sep.widthAnchor constraintEqualToConstant:1],
        [self.picker.view.leadingAnchor constraintEqualToAnchor:sep.trailingAnchor], [self.picker.view.topAnchor constraintEqualToAnchor:root.topAnchor], [self.picker.view.bottomAnchor constraintEqualToAnchor:root.bottomAnchor],
        [self.picker.view.widthAnchor constraintEqualToConstant:kSide], [self.picker.view.trailingAnchor constraintEqualToAnchor:root.trailingAnchor],
    ]];
    self.rootView = root;
    w.contentView = root;
    self.win = w;
    [self modeChanged:nil];
    [self relayout];
    [w center];
    [w makeKeyAndOrderFront:nil];
    [NSApp activateIgnoringOtherApps:YES];
}

- (void)relayout {
    [self.content layoutSubtreeIfNeeded];
    NSSize sz = self.content.fittingSize;
    [self.win setContentSize:NSMakeSize(kW + 48 + 1 + kSide, MAX(sz.height, 520))];
    [self snapshot];
}

- (void)snapshot {                                                // только для автоматических проверок: рисует окно в PNG
    NSString *p = NSProcessInfo.processInfo.environment[@"REAPER_PTX_TEST_SNAP"];
    if (!p.length) return;
    After(0.8, ^{
        NSView *v = self.win.contentView.superview ?: self.win.contentView;
        NSBitmapImageRep *rep = [v bitmapImageRepForCachingDisplayInRect:v.bounds];
        [v cacheDisplayInRect:v.bounds toBitmapImageRep:rep];
        [[rep representationUsingType:NSBitmapImageFileTypePNG properties:@{}] writeToFile:p atomically:YES];
    });
}

- (double)sessionFps { double f = [self.info[@"fps"] doubleValue]; return f > 1 ? f : 30; }

- (BOOL)currentOffset:(double *)out {
    BOOL ok = NO;
    double v = ParseOffset(self.offsetField.stringValue, self.offsetFormat.indexOfSelectedItem, [self sessionFps], &ok);
    if (out) *out = ok ? v : 0;
    return ok;
}

- (void)offsetEdited:(id)sender {
    double v; BOOL ok = [self currentOffset:&v];
    self.offsetNote.textColor = ok ? NSColor.secondaryLabelColor : NSColor.systemRedColor;
    self.offsetNote.stringValue = ok ? (v == 0 ? @"Shifts everything (clips, automation, markers) in time. Type digits; start with - for an earlier time."
                                                : [NSString stringWithFormat:@"Everything will be moved %@ by %.3f s.", v < 0 ? @"earlier" : @"later", fabs(v)])
                                     : @"Cannot read this time.";
    if (self.info && !self.task.isRunning) self.importButton.enabled = ok && (!self.picker.tracks.count || self.picker.selected.count > 0);
}

- (void)offsetFormatChanged:(id)sender {                       // значение переводится в выбранный формат
    static NSInteger prev = PXOffsetTimecode;
    BOOL ok = NO;
    double v = ParseOffset(self.offsetField.stringValue, prev, [self sessionFps], &ok);
    if (ok && self.offsetField.stringValue.length) self.offsetField.stringValue = FormatOffset(v, self.offsetFormat.indexOfSelectedItem, [self sessionFps]);
    self.offsetField.placeholderString = self.offsetFormat.indexOfSelectedItem == PXOffsetTimecode ? @"00:00:00:00" : self.offsetFormat.indexOfSelectedItem == PXOffsetMinSec ? @"000:00.000" : @"0.000";
    prev = self.offsetFormat.indexOfSelectedItem;
    [self offsetEdited:nil];
}

- (void)offsetReset:(id)sender { self.offsetField.stringValue = @""; [self offsetEdited:nil]; }

- (void)matchStart:(id)sender {                                // сдвиг, при котором таймкоды клипов в Reaper совпадают с таймкодами Pro Tools
    double ss = [self.info[@"start_seconds"] doubleValue], po = f_GetProjectTimeOffset ? f_GetProjectTimeOffset(NULL, false) : 0;
    self.offsetField.stringValue = FormatOffset(ss - po, self.offsetFormat.indexOfSelectedItem, [self sessionFps]);
    [self offsetEdited:nil];
}

- (void)controlTextDidChange:(NSNotification *)n {
    if (n.object != self.offsetField) return;
    NSString *m = MaskOffsetText(self.offsetField.stringValue, self.offsetFormat.indexOfSelectedItem, [self sessionFps]);
    if (![m isEqualToString:self.offsetField.stringValue]) {
        self.offsetField.stringValue = m;
        NSText *ed = self.offsetField.currentEditor;
        if (ed) ed.selectedRange = NSMakeRange(m.length, 0);
    }
    [self offsetEdited:nil];
}

- (void)syncModeButtons {
    BOOL copy = self.mode.selectedSegment == 1;
    self.mediaCopyBtn.state = copy ? NSControlStateValueOn : NSControlStateValueOff;
    self.linkBtn.state = copy ? NSControlStateValueOff : NSControlStateValueOn;
}

- (void)modeChecked:(NSButton *)b {                            // две галочки работают как переключатель: одна всегда включена
    self.mode.selectedSegment = (b == self.mediaCopyBtn) ? 1 : 0;
    [self modeChanged:nil];
}

- (void)setModeEnabled:(BOOL)on { self.mediaCopyBtn.enabled = on; self.linkBtn.enabled = on; }

- (void)modeChanged:(id)sender {
    [self syncModeButtons];
    self.outputNote.stringValue = self.mode.selectedSegment == 0
        ? @"Reaper will reference the original PTX session audio files by absolute path. Nothing is copied; the import breaks if the PTX session moves."
        : @"The audio used by the session is copied into the project's media folder, so the import is self-contained.";
    if (self.info) [self showInfo:self.info];
}

- (NSTask *)runEngine:(NSArray *)args lineHandler:(void (^)(NSString *))handler completion:(void (^)(int, NSString *))done {
    NSTask *t = [NSTask new];
    t.executableURL = [NSURL fileURLWithPath:kPython];
    NSMutableArray *a = [NSMutableArray arrayWithObject:[self.engine stringByAppendingPathComponent:@"ptx_to_reaper.py"]];
    [a addObjectsFromArray:args];
    t.arguments = a;
    NSPipe *pipe = [NSPipe pipe];
    t.standardOutput = pipe;
    t.standardError = pipe;
    NSMutableString *all = [NSMutableString string];
    __block NSMutableString *buf = [NSMutableString string];
    pipe.fileHandleForReading.readabilityHandler = ^(NSFileHandle *h) {
        NSData *d = h.availableData;
        if (!d.length) return;
        NSString *s = [[NSString alloc] initWithData:d encoding:NSUTF8StringEncoding] ?: @"";
        @synchronized (all) { [all appendString:s]; }
        [buf appendString:s];
        NSRange r;
        while ((r = [buf rangeOfString:@"\n"]).location != NSNotFound) {
            NSString *line = [buf substringToIndex:r.location];
            [buf deleteCharactersInRange:NSMakeRange(0, r.location + 1)];
            if (handler) dispatch_async(dispatch_get_main_queue(), ^{ handler(line); });
        }
    };
    t.terminationHandler = ^(NSTask *tt) {
        pipe.fileHandleForReading.readabilityHandler = nil;
        NSData *rest = [pipe.fileHandleForReading readDataToEndOfFile];
        if (rest.length) { NSString *s = [[NSString alloc] initWithData:rest encoding:NSUTF8StringEncoding]; if (s) @synchronized (all) { [all appendString:s]; } }
        NSString *out;
        @synchronized (all) { out = [all copy]; }
        dispatch_async(dispatch_get_main_queue(), ^{ done(tt.terminationStatus, out); });
    };
    NSError *err = nil;
    if (![t launchAndReturnError:&err]) { dispatch_async(dispatch_get_main_queue(), ^{ done(-1, err.localizedDescription ?: @"launch failed"); }); return nil; }
    return t;
}

- (NSMutableArray *)relinkArgs {
    NSMutableArray *a = [NSMutableArray array];
    NSString *f = RelinkJSONFile(self.relinkMap);
    if (f) { [a addObject:@"--relink"]; [a addObject:f]; }
    return a;
}

- (NSMutableArray *)optionArgs {
    NSMutableArray *a = [NSMutableArray array], *off = [NSMutableArray array], *on = [NSMutableArray array];
    NSDictionary *flag = @{@"fades": @"--no-fades", @"clip_gain": @"--no-gain", @"markers": @"--no-markers", @"send_routing": @"--no-sends",
                           @"main_routing": @"--no-folders", @"fader": @"--no-fader", @"plugins": @"--no-plugins", @"aux": @"--no-aux"};
    NSSet *offKeys = [NSSet setWithArray:@[@"video", @"groups", @"vca", @"clips", @"clip_mute", @"clip_colors", @"track_colors", @"notes", @"solo", @"mute", @"auto_vol", @"auto_pan", @"auto_mute",
                                           @"send_auto_vol", @"send_auto_pan", @"send_auto_mute"]];
    for (NSString *k in flag) if (![self.opts[k] boolValue]) [a addObject:flag[k]];
    for (NSString *k in offKeys) if (![self.opts[k] boolValue]) [off addObject:k];
    for (NSString *k in @[@"skip_hidden", @"skip_inactive", @"trim_overlap"]) if ([self.opts[k] boolValue]) [on addObject:k];
    if (off.count) { [a addObject:@"--off"]; [a addObject:[off componentsJoinedByString:@","]]; }
    if (on.count) { [a addObject:@"--on"]; [a addObject:[on componentsJoinedByString:@","]]; }
    if (![self.opts[@"playlists"] boolValue]) [a addObject:@"--no-alt-playlists"];
    else if (![self.opts[@"lanes"] boolValue]) [a addObject:@"--alt-as-tracks"];
    return a;
}

- (void)loadInfo {
    [self spinOn];
    self.importButton.enabled = NO;
    NSMutableArray *args = [NSMutableArray arrayWithObjects:self.ptxPath, @"--info", nil];
    [args addObjectsFromArray:[self relinkArgs]];
    self.task = [self runEngine:args lineHandler:nil completion:^(int code, NSString *out) {
        [self spinOff];
        NSDictionary *j = nil;
        for (NSString *ln in [out componentsSeparatedByString:@"\n"]) {
            if ([ln hasPrefix:@"{"]) j = [NSJSONSerialization JSONObjectWithData:[ln dataUsingEncoding:NSUTF8StringEncoding] options:0 error:nil];
        }
        if (code != 0 || !j) {
            [self setBlock:self.inputBlock rows:@[Label([NSString stringWithFormat:@"Session file: %@", self.ptxPath.lastPathComponent], NO, 12, nil)]];
            [self setBlock:self.preflightBlock rows:@[IconRow(@"exclamationmark.triangle.fill", NSColor.systemYellowColor, @"Could not read this session.", nil),
                                                       Label([out stringByTrimmingCharactersInSet:NSCharacterSet.whitespaceAndNewlineCharacterSet], NO, 11, NSColor.secondaryLabelColor)]];
            [self relayout];
            return;
        }
        self.info = j;
        [self showInfo:j];
    }];
}

- (void)setBlock:(NSStackView *)block rows:(NSArray *)rows {
    for (NSView *v in [block.arrangedSubviews copy]) { [block removeArrangedSubview:v]; [v removeFromSuperview]; }
    for (NSView *r in rows) [block addArrangedSubview:r];
}

static NSTextField *SubHeader(NSString *s) { return Label(s, YES, 10, NSColor.tertiaryLabelColor); }

- (void)showInfo:(NSDictionary *)j {
    NSMutableArray *in = [NSMutableArray array];
    NSColor *grey = NSColor.secondaryLabelColor;
    [in addObject:Label([NSString stringWithFormat:@"Session file:  %@", self.ptxPath.lastPathComponent], NO, 12, grey)];
    if ([j[@"app_info"] isKindOfClass:[NSString class]]) [in addObject:Label(j[@"app_info"], NO, 12, grey)];
    NSMutableString *l2 = [NSMutableString stringWithFormat:@"%@ Hz", Num(j[@"rate"])];
    if ([j[@"bits"] isKindOfClass:[NSNumber class]]) [l2 appendFormat:@", %@-bit", j[@"bits"]];
    if ([j[@"fps"] isKindOfClass:[NSNumber class]]) [l2 appendFormat:@", %@ fps", j[@"fps"]];
    if ([j[@"start_tc"] isKindOfClass:[NSString class]]) [l2 appendFormat:@", start %@", j[@"start_tc"]];
    [in addObject:Label(l2, NO, 12, grey)];
    int audioFiles = j[@"files_used"] ? [j[@"files_used"] intValue] : [j[@"files"] intValue] - [j[@"video_files"] intValue];
    [in addObject:Label([NSString stringWithFormat:@"%@ tracks, %@ clips, %@ fades, %d files, %@ vol points, %@ clip gain", Num(j[@"audio_tracks"] ?: j[@"tracks"]), Num(j[@"clips_raw"] ?: j[@"clips"]), Num(j[@"fades_raw"] ?: j[@"fades"]), audioFiles, Num(j[@"vol_points"]), Num(j[@"clip_gain_entries"])], NO, 12, grey)];
    if ([j[@"video_tracks"] intValue] > 0 || [j[@"video_files"] intValue] > 0)
        [in addObject:Label([NSString stringWithFormat:@"Video: %@ track, %@ clip", Num(j[@"video_tracks"]), Num(j[@"video_clips"])], NO, 12, grey)];
    [self setBlock:self.inputBlock rows:in];

    NSArray *missing = j[@"missing_detail"] ?: @[];
    int files = [j[@"files"] intValue], skipped = [j[@"skipped"] intValue];
    BOOL standalone = self.mode.selectedSegment == 1;
    NSMutableArray *pf = [NSMutableArray array];
    int found = files - (int)missing.count;
    // 1) не найденные аудиофайлы — самый первый блок Preflight
    NSView *missingRow = nil;
    if (missing.count) {
        NSButton *relink = [NSButton buttonWithTitle:@"Relink…" target:self action:@selector(relink:)];
        relink.bezelStyle = NSBezelStyleSmallSquare; relink.controlSize = NSControlSizeSmall; relink.font = [NSFont systemFontOfSize:11];
        missingRow = IconRow(@"exclamationmark.triangle.fill", NSColor.systemYellowColor,
                             [NSString stringWithFormat:@"%lu audio file%@ not found — their clips stay offline; use Relink to point to the files.", (unsigned long)missing.count, missing.count == 1 ? @"" : @"s"], relink);
        [pf addObject:missingRow];
    }
    // 2) слинкованные файлы и автоматические преобразования
    NSView *linked = nil;
    if (found > 0) {
        linked = IconRow(@"checkmark.circle.fill", NSColor.systemGreenColor,
                         [NSString stringWithFormat:@"%d file%@ %@", found, found == 1 ? @"" : @"s", standalone ? @"will be copied" : @"linked"], nil);
        [pf addObject:linked];
    }
    NSView *autoH = SubHeader(@"AUTOMATIC CONVERSIONS");
    NSView *autoT = Label(@"The converter will create these files for you.", NO, 11, NSColor.secondaryLabelColor);
    NSView *autoR = IconRow(@"checkmark.circle.fill", NSColor.systemGreenColor,
                            standalone ? @"Source audio is copied unchanged." : @"Nothing to process — all source files are ready for direct use.", nil);
    for (NSView *v in @[autoH, autoT, autoR]) [pf addObject:v];
    // 3) прочие предупреждения совместимости (раздел появляется, если есть что показать или нет проблем вообще)
    NSMutableArray *warns = [NSMutableArray array];
    if (skipped) [warns addObject:IconRow(@"exclamationmark.triangle.fill", NSColor.systemYellowColor, [NSString stringWithFormat:@"%d clip%@ could not be read and will be skipped.", skipped, skipped == 1 ? @"" : @"s"], nil)];
    if ([j[@"skipped_tracks"] intValue]) {
        NSMutableArray *names = [NSMutableArray array];
        for (NSDictionary *d in ([j[@"skipped_detail"] isKindOfClass:[NSArray class]] ? j[@"skipped_detail"] : @[])) [names addObject:[NSString stringWithFormat:@"\u201c%@\u201d (%@)", d[@"name"], d[@"reason"]]];
        NSString *msg = names.count ? [NSString stringWithFormat:@"Not transferred: %@.", [names componentsJoinedByString:@", "]]
                                    : [NSString stringWithFormat:@"%@ track(s) of unsupported types are skipped.", Num(j[@"skipped_tracks"])];
        [warns addObject:IconRow(@"info.circle.fill", NSColor.systemBlueColor, msg, nil)];
    }
    if ([j[@"track_markers"] intValue]) [warns addObject:IconRow(@"info.circle.fill", NSColor.systemBlueColor, [NSString stringWithFormat:@"%@ marker(s) on tracks become empty items on those tracks (Reaper has no track markers).", Num(j[@"track_markers"])], nil)];
    if (f_GetProjectTimeOffset && j[@"start_tc"]) {
        double ss = [j[@"start_seconds"] doubleValue], po = f_GetProjectTimeOffset(NULL, false);
        if (fabs(ss - po) > 0.001) {
            double fps = [j[@"fps"] doubleValue] > 1 ? [j[@"fps"] doubleValue] : 30;
            NSButton *match = [NSButton buttonWithTitle:@"Match" target:self action:@selector(matchStart:)];
            match.bezelStyle = NSBezelStyleSmallSquare; match.controlSize = NSControlSizeSmall; match.font = [NSFont systemFontOfSize:11];
            match.toolTip = @"Set the time offset so that clips keep their Pro Tools timecodes in this project";
            [warns addObject:IconRow(@"exclamationmark.triangle.fill", NSColor.systemYellowColor,
                                     [NSString stringWithFormat:@"The session starts at %@, this project starts at %@ — clips would land on different timecodes than in Pro Tools.", FormatOffset(ss, PXOffsetTimecode, fps), FormatOffset(po, PXOffsetTimecode, fps)], match)];
        }
    }
    if (f_GetSetProjectInfo) {
        double prate = f_GetSetProjectInfo(NULL, "PROJECT_SRATE", 0, false);
        if (prate > 0 && fabs(prate - [j[@"rate"] doubleValue]) > 1)
            [warns addObject:IconRow(@"exclamationmark.triangle.fill", NSColor.systemYellowColor, [NSString stringWithFormat:@"The open project runs at %.0f Hz, the session at %@ Hz.", prate, Num(j[@"rate"])], nil)];
    }
    if (!warns.count && !missing.count) [warns addObject:IconRow(@"checkmark.circle.fill", NSColor.systemGreenColor, @"No compatibility issues detected.", nil)];
    NSView *warnH = nil;
    if (warns.count) {
        warnH = SubHeader(@"COMPATIBILITY WARNINGS");
        [pf addObject:warnH];
        [pf addObject:Label(@"Review these before converting.", NO, 11, NSColor.secondaryLabelColor)];
        for (NSView *v in warns) [pf addObject:v];
    }
    [self setBlock:self.preflightBlock rows:pf];
    if (missingRow) [self.preflightBlock setCustomSpacing:10 afterView:missingRow];
    if (linked) [self.preflightBlock setCustomSpacing:10 afterView:linked];
    [self.preflightBlock setCustomSpacing:2 afterView:autoH];
    [self.preflightBlock setCustomSpacing:10 afterView:autoR];
    if (warnH) [self.preflightBlock setCustomSpacing:2 afterView:warnH];

    if ([j[@"track_list"] isKindOfClass:[NSArray class]]) [self.picker setTracks:j[@"track_list"]];
    self.importButton.enabled = !self.picker.tracks.count || self.picker.selected.count > 0;
    NSString *testLog = NSProcessInfo.processInfo.environment[@"REAPER_PTX_TEST_LOG"];       // только для автоматических проверок
    if (testLog.length && !self.statusLabel.stringValue.length) {
        NSString *dump = [NSString stringWithFormat:@"INFO %@\nMISSING %lu\nRELINKED %lu\nWINDOW %@\nPROJOFFS fn=%d value=%f\n", [[NSString alloc] initWithData:[NSJSONSerialization dataWithJSONObject:j options:0 error:nil] encoding:NSUTF8StringEncoding], (unsigned long)missing.count, (unsigned long)self.relinkMap.count, self.win.title, f_GetProjectTimeOffset != NULL, f_GetProjectTimeOffset ? f_GetProjectTimeOffset(NULL, false) : -1.0];
        [dump writeToFile:testLog atomically:YES encoding:NSUTF8StringEncoding error:nil];
        if (NSProcessInfo.processInfo.environment[@"REAPER_PTX_TEST_DATA_SNAP"]) After(1.5, ^{ [self showTrackData:nil]; });
        if ([NSProcessInfo.processInfo.environment[@"REAPER_PTX_TEST_MODE"] isEqualToString:@"link"]) { self.mode.selectedSegment = 0; [self syncModeButtons]; }
        NSString *toff = NSProcessInfo.processInfo.environment[@"REAPER_PTX_TEST_OFFSET"];
        if (toff.length) { self.offsetField.stringValue = MaskOffsetText(toff, self.offsetFormat.indexOfSelectedItem, [self sessionFps]); [self offsetEdited:nil]; }
        NSString *tfil = NSProcessInfo.processInfo.environment[@"REAPER_PTX_TEST_FILTER"];
        if (tfil.length) { self.picker.search.stringValue = tfil; [self.picker applyFilter]; }
        NSString *tsel = NSProcessInfo.processInfo.environment[@"REAPER_PTX_TEST_TRACKS"];
        if (tsel.length) { [self.picker.selected removeAllObjects]; for (NSString *x in [tsel componentsSeparatedByString:@","]) [self.picker.selected addObject:@(x.intValue)]; [self.picker.table reloadData]; [self.picker refreshCount]; }
        if (NSProcessInfo.processInfo.environment[@"REAPER_PTX_TEST_RELINK_SNAP"] && !self.relinkMap.count) After(2, ^{ [self relink:nil]; });
        else if (NSProcessInfo.processInfo.environment[@"REAPER_PTX_TEST_AUTOIMPORT"]) dispatch_async(dispatch_get_main_queue(), ^{ [self doImport:nil]; });
    }
    [self relayout];
}

- (void)relink:(id)sender {
    NSArray *missing = self.info[@"missing_detail"] ?: @[];
    __weak PXImporter *ws = self;
    PXRunner runner = ^NSTask *(NSArray *args, void (^done)(int, NSString *)) { return [ws runEngine:args lineHandler:nil completion:done]; };
    PXRelink *r = [[PXRelink alloc] initWithMissing:missing existing:self.relinkMap ptx:self.ptxPath runner:runner];
    self.relinkWin = r;
    NSDictionary *chosen = [r runModalResult];
    self.relinkWin = nil;
    if (!chosen.count) return;
    [self.relinkMap addEntriesFromDictionary:chosen];
    self.info = nil;
    [self loadInfo];
}

- (void)showTrackData:(id)sender {
    NSStackView *st = VStack(5);
    st.edgeInsets = NSEdgeInsetsMake(14, 20, 14, 20);
    [st addArrangedSubview:Label(@"Track Data", YES, 13, nil)];
    for (NSArray *g in TrackDataGroups()) {
        NSBox *sep = [NSBox new]; sep.boxType = NSBoxSeparator;
        sep.translatesAutoresizingMaskIntoConstraints = NO;
        [st addArrangedSubview:sep];
        [sep.widthAnchor constraintEqualToConstant:300].active = YES;
        for (NSArray *it in g) {
            NSButton *cb = [NSButton checkboxWithTitle:it[1] target:self action:@selector(optToggled:)];
            cb.identifier = it[0];
            BOOL supported = [it[3] boolValue];
            cb.state = supported && [self.opts[it[0]] boolValue] ? NSControlStateValueOn : NSControlStateValueOff;
            cb.enabled = supported;
            if (!supported) cb.toolTip = @"Not supported yet";
            [st addArrangedSubview:cb];
            if ([it[0] isEqualToString:@"playlists"]) {
                NSSegmentedControl *lanes = [NSSegmentedControl segmentedControlWithLabels:@[@"As Takes & Lanes", @"As muted tracks"] trackingMode:NSSegmentSwitchTrackingSelectOne target:self action:@selector(lanesChanged:)];
                lanes.selectedSegment = [self.opts[@"lanes"] boolValue] ? 0 : 1;
                lanes.controlSize = NSControlSizeSmall;
                [st addArrangedSubview:lanes];
            }
        }
    }
    NSButton *ok = [NSButton buttonWithTitle:@"Done" target:self action:@selector(closeSheet:)];
    ok.keyEquivalent = @"\r";
    NSScrollView *sv = [NSScrollView new];
    st.translatesAutoresizingMaskIntoConstraints = NO;
    NSView *doc = [NSView new];
    [doc addSubview:st];
    [st.leadingAnchor constraintEqualToAnchor:doc.leadingAnchor].active = YES;
    [st.topAnchor constraintEqualToAnchor:doc.topAnchor].active = YES;
    [st.trailingAnchor constraintEqualToAnchor:doc.trailingAnchor].active = YES;
    [st.bottomAnchor constraintEqualToAnchor:doc.bottomAnchor].active = YES;
    [doc layoutSubtreeIfNeeded];
    NSSize fit = st.fittingSize;
    doc.frame = NSMakeRect(0, 0, fit.width, fit.height);
    sv.documentView = doc;
    sv.hasVerticalScroller = YES;
    sv.drawsBackground = NO;
    CGFloat h = MIN(fit.height, NSScreen.mainScreen.visibleFrame.size.height - 170);
    NSStackView *outer = VStack(10);
    outer.edgeInsets = NSEdgeInsetsMake(0, 0, 16, 0);
    sv.translatesAutoresizingMaskIntoConstraints = NO;
    [sv.widthAnchor constraintEqualToConstant:fit.width + 14].active = YES;
    [sv.heightAnchor constraintEqualToConstant:h].active = YES;
    [outer addArrangedSubview:sv];
    [sv.contentView scrollToPoint:NSMakePoint(0, MAX(0, fit.height - h))];
    [sv reflectScrolledClipView:sv.contentView];
    NSView *flex = [NSView new];
    [flex setContentHuggingPriority:NSLayoutPriorityDefaultLow forOrientation:NSLayoutConstraintOrientationHorizontal];
    NSStackView *doneRow = [NSStackView stackViewWithViews:@[flex, ok]];
    doneRow.edgeInsets = NSEdgeInsetsMake(0, 20, 0, 24);
    doneRow.translatesAutoresizingMaskIntoConstraints = NO;
    [outer addArrangedSubview:doneRow];
    [doneRow.widthAnchor constraintEqualToAnchor:sv.widthAnchor].active = YES;
    NSWindow *sheet = [[NSWindow alloc] initWithContentRect:NSMakeRect(0, 0, 340, 380) styleMask:NSWindowStyleMaskTitled backing:NSBackingStoreBuffered defer:NO];
    sheet.contentView = outer;
    [outer layoutSubtreeIfNeeded];
    [sheet setContentSize:outer.fittingSize];
    self.optSheet = sheet;
    [self.win beginSheet:sheet completionHandler:nil];
    NSString *snap = NSProcessInfo.processInfo.environment[@"REAPER_PTX_TEST_DATA_SNAP"];                 // только для автоматических проверок
    if (snap.length) After(1, ^{
        NSView *v = sheet.contentView.superview;
        NSBitmapImageRep *rep = [v bitmapImageRepForCachingDisplayInRect:v.bounds];
        [v cacheDisplayInRect:v.bounds toBitmapImageRep:rep];
        [[rep representationUsingType:NSBitmapImageFileTypePNG properties:@{}] writeToFile:snap atomically:YES];
    });
}

- (void)optToggled:(NSButton *)b { self.opts[b.identifier] = @(b.state == NSControlStateValueOn); }
- (void)lanesChanged:(NSSegmentedControl *)s { self.opts[@"lanes"] = @(s.selectedSegment == 0); }
- (void)closeSheet:(id)sender { [self.win endSheet:self.optSheet]; self.optSheet = nil; }

- (void)cancel:(id)sender {
    if (self.task.isRunning) [self.task terminate];
    [self.win close];
}

- (void)windowWillClose:(NSNotification *)n { if (self.task.isRunning) [self.task terminate]; g_importer = nil; }

- (void)doImport:(id)sender {
    NSString *base = [self.ptxPath.lastPathComponent stringByDeletingPathExtension];
    NSString *tmp = [NSTemporaryDirectory() stringByAppendingPathComponent:[NSString stringWithFormat:@"ptx_import_%@.rpp", NSUUID.UUID.UUIDString]];
    NSMutableArray *args = [NSMutableArray arrayWithObjects:self.ptxPath, @"--out", tmp, nil];
    [args addObjectsFromArray:[self relinkArgs]];
    [args addObjectsFromArray:[self optionArgs]];
    double offv = 0;
    if (![self currentOffset:&offv]) { self.statusLabel.stringValue = @"Fix the time offset first."; return; }
    if (offv != 0) { [args addObject:@"--offset"]; [args addObject:[NSString stringWithFormat:@"%.6f", offv]]; }
    NSSet *chosenTracks = [self.picker selectedOrNil];
    if (chosenTracks) {
        NSArray *ix = [[chosenTracks allObjects] sortedArrayUsingSelector:@selector(compare:)];
        [args addObject:@"--tracks"]; [args addObject:[ix componentsJoinedByString:@","]];
    }
    if (self.mode.selectedSegment == 1) {                         // Standalone: аудио — в папку медиа открытого проекта
        char pbuf[4096] = {0};
        if (f_GetProjectPath) f_GetProjectPath(pbuf, sizeof pbuf);
        NSString *dir = pbuf[0] ? [NSString stringWithUTF8String:pbuf] : nil;
        if (!dir.length) {
            NSOpenPanel *op = [NSOpenPanel openPanel];
            op.title = @"The project is not saved yet — choose a folder for the copied audio";
            op.canChooseFiles = NO; op.canChooseDirectories = YES; op.canCreateDirectories = YES;
            if ([op runModal] != NSModalResponseOK) return;
            dir = op.URL.path;
        }
        [args addObject:@"--copy-media-to"];
        [args addObject:[dir stringByAppendingPathComponent:[base stringByAppendingString:@" (PT media)"]]];
    }
    self.importButton.enabled = NO;
    [self setModeEnabled:NO];
    self.dataButton.enabled = NO;
    [self spinOn];
    self.statusLabel.stringValue = @"Starting…";
    __block BOOL done = NO;
    NSArray *wanted = [self.info[@"fx_wanted"] isKindOfClass:[NSArray class]] ? self.info[@"fx_wanted"] : @[];
    NSString *reqPath = [NSTemporaryDirectory() stringByAppendingPathComponent:[NSString stringWithFormat:@"px_param_requests_%@.json", NSUUID.UUID.UUIDString]];
    [args addObject:@"--param-requests"]; [args addObject:reqPath];
    void (^launch)(NSString *) = ^(NSString *libPath) {
    if (libPath) { [args addObject:@"--fx-library"]; [args addObject:libPath]; }
    self.task = [self runEngine:args lineHandler:^(NSString *line) {
        if ([line hasPrefix:@"Done:"]) done = YES;
        else if (line.length && ![line hasPrefix:@"{"]) self.statusLabel.stringValue = line;
    } completion:^(int code, NSString *outText) {
        [self spinOff];
        NSString *text = [NSString stringWithContentsOfFile:tmp encoding:NSUTF8StringEncoding error:nil];
        [[NSFileManager defaultManager] removeItemAtPath:tmp error:nil];
        if (code != 0 || !done || !text) {
            self.statusLabel.stringValue = [NSString stringWithFormat:@"Import failed: %@", [outText componentsSeparatedByString:@"\n"].lastObject ?: @""];
            self.importButton.enabled = YES; [self setModeEnabled:YES]; self.dataButton.enabled = YES;
            return;
        }
        CFAbsoluteTime tParse = CFAbsoluteTimeGetCurrent();
        PXProject *proj = ParseRPP(text);
        NSMutableArray *notes = [NSMutableArray array];
        CFAbsoluteTime tIns = CFAbsoluteTimeGetCurrent();
        [self spinOn];
        NSData *rq = [NSData dataWithContentsOfFile:reqPath]; [[NSFileManager defaultManager] removeItemAtPath:reqPath error:nil];
        id rqj = rq ? [NSJSONSerialization JSONObjectWithData:rq options:0 error:nil] : nil;
        InsertIntoProjectAsync(proj, self.ptxPath.lastPathComponent, notes, [rqj isKindOfClass:[NSArray class]] ? rqj : @[], ^(NSString *st) { self.statusLabel.stringValue = st; }, ^(int n) {
            [self spinOff];
            NSString *log = NSProcessInfo.processInfo.environment[@"REAPER_PTX_TEST_LOG"];
            if (log.length) {
                NSString *d = [NSString stringWithFormat:@"TIMING %@ (parse %.2fs)\nIMPORTED %d tracks (project now has %d)\nNOTES %@\n", g_timing, tIns - tParse, n, f_CountTracks ? f_CountTracks(NULL) : -1, [notes componentsJoinedByString:@" | "]];
                NSFileHandle *fh = [NSFileHandle fileHandleForWritingAtPath:log];
                [fh seekToEndOfFile]; [fh writeData:[d dataUsingEncoding:NSUTF8StringEncoding]]; [fh closeFile];
            }
            NSString *sv = NSProcessInfo.processInfo.environment[@"REAPER_PTX_TEST_SAVE"];      // только для автоматических проверок
            if (sv.length && f_Main_SaveProjectEx) f_Main_SaveProjectEx(NULL, sv.UTF8String, 0);
            [self.win close];
            if (n < 0) {
                NSAlert *a = [NSAlert new]; a.messageText = @"Import failed"; a.informativeText = @"This version of Reaper does not provide the functions needed to insert tracks."; [a runModal];
            } else if (notes.count) {
                NSAlert *a = [NSAlert new]; a.messageText = [NSString stringWithFormat:@"Imported %d tracks", n]; a.informativeText = [notes componentsJoinedByString:@"\n"]; [a runModal];
            }
        });
    }];
    };
    if (wanted.count) HarvestFX(wanted, ^(NSString *st) { self.statusLabel.stringValue = st; }, ^(NSString *p) { launch(p); });
    else launch(nil);
}

@end

// ------------------------------------------------------------------------------------------------- меню и точка входа

@interface PXMenuTarget : NSObject
- (void)run:(id)sender;
@end
@implementation PXMenuTarget
- (void)run:(id)sender {
    if (g_importer) { [g_importer.win makeKeyAndOrderFront:nil]; return; }
    g_importer = [PXImporter new];
    [g_importer start];
    if (!g_importer.win) g_importer = nil;
}
@end

static PXMenuTarget *g_target = nil;

static bool HookCommand(int command, int flag) {
    if (command == g_cmdId && g_cmdId) { [g_target run:nil]; return true; }
    return false;
}

static void InsertItem(NSMenu *menu) {
    for (NSMenuItem *it in menu.itemArray) if ([it.title hasPrefix:@"Import Pro Tools Session"]) return;
    NSInteger idx = -1;
    for (NSInteger i = 0; i < menu.numberOfItems; i++) if ([menu.itemArray[i].title hasPrefix:@"Open project"]) { idx = i + 1; break; }
    if (idx < 0) idx = MIN(3, menu.numberOfItems);
    NSMenuItem *mi = [[NSMenuItem alloc] initWithTitle:@"Import Pro Tools Session…" action:@selector(run:) keyEquivalent:@""];
    mi.target = g_target;
    [menu insertItem:mi atIndex:idx];
}

static void MenuHook(const char *menuidstr, void *hmenu, int flag) {
    if (!menuidstr || strcmp(menuidstr, "Main file") != 0 || !hmenu) return;
    NSMenu *m = (__bridge NSMenu *)hmenu;
    if ([m isKindOfClass:[NSMenu class]]) InsertItem(m);
}

#define GETF(name) f_##name = rec->GetFunc(#name)

__attribute__((visibility("default")))
int ReaperPluginEntry(void *hInstance, px_reaper_plugin_info_t *rec) {
    if (!rec) return 0;
    GETF(InsertTrackAtIndex); GETF(GetTrack); GETF(CountTracks); GETF(SetTrackStateChunk); GETF(GetTrackStateChunk); GETF(AddMediaItemToTrack); GETF(SetItemStateChunk);
    GETF(GetMasterTrack); GETF(SetMediaTrackInfo_Value); GETF(GetMediaTrackInfo_Value); GETF(TrackFX_GetCount); GETF(TrackFX_AddByName); GETF(TrackFX_GetNamedConfigParm); GETF(TrackFX_SetNamedConfigParm); GETF(TrackFX_Delete); GETF(DeleteTrack); GETF(TrackFX_GetNumParams); GETF(TrackFX_GetParamName); GETF(TrackFX_SetParamNormalized); GETF(TrackFX_GetParameterStepSizes);
    GETF(AddProjectMarker2); GETF(GetSetProjectNotes); GETF(Undo_BeginBlock2); GETF(Undo_EndBlock2); GETF(UpdateArrange);
    GETF(PreventUIRefresh); GETF(TrackList_AdjustWindows); GETF(GetProjectPath); GETF(GetProjectTimeOffset); GETF(GetSetProjectInfo); GETF(Main_SaveProjectEx); GETF(GetSetTrackGroupMembership); GETF(GetSetMediaTrackInfo_String);
    if (!f_InsertTrackAtIndex || !f_SetTrackStateChunk) return 0;
    g_target = [PXMenuTarget new];
    g_cmdId = rec->Register("command_id", (void *)"PT_SESSION_IMPORT_OURS");
    static px_gaccel_register_t accel;
    accel.accel.fVirt = 0; accel.accel.key = 0; accel.accel.cmd = (unsigned short)g_cmdId;
    accel.desc = "Import Pro Tools session…";
    rec->Register("gaccel", &accel);
    rec->Register("hookcommand", (void *)HookCommand);
    rec->Register("hookcustommenu", (void *)MenuHook);
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(3 * NSEC_PER_SEC)), dispatch_get_main_queue(), ^{   // запасной путь
        for (NSMenuItem *top in NSApp.mainMenu.itemArray) if ([top.title isEqualToString:@"File"] && top.submenu) InsertItem(top.submenu);
    });
    if (NSProcessInfo.processInfo.environment[@"REAPER_PTX_TEST_PTX"].length)                                       // только для автоматических проверок
        dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(6 * NSEC_PER_SEC)), dispatch_get_main_queue(), ^{ [g_target run:nil]; });
    if (NSProcessInfo.processInfo.environment[@"REAPER_PTX_TEST_VCA"].length)                                        // только для автоматических проверок: образец VCA
        dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(6 * NSEC_PER_SEC)), dispatch_get_main_queue(), ^{
            int base = f_CountTracks(NULL);
            for (int i = 0; i < 4; i++) { f_InsertTrackAtIndex(base + i, false); MediaTrack t = f_GetTrack(NULL, base + i); char nm[16]; snprintf(nm, sizeof nm, i == 0 ? "VCA lead" : "Follower %d", i); f_GetSetMediaTrackInfo_String(t, "P_NAME", nm, true); }
            MediaTrack lead = f_GetTrack(NULL, base);
            f_GetSetTrackGroupMembership(lead, "VOLUME_VCA_LEAD", 1, 1);          // группа 1: ведущий
            f_GetSetTrackGroupMembership(lead, "MUTE_VCA_LEAD", 1, 1);
            for (int i = 1; i <= 2; i++) { MediaTrack t = f_GetTrack(NULL, base + i); f_GetSetTrackGroupMembership(t, "VOLUME_VCA_FOLLOW", 1, 1); f_GetSetTrackGroupMembership(t, "MUTE_VCA_FOLLOW", 1, 1); }
            f_SetMediaTrackInfo_Value(lead, "D_VOL", 0.5);
            f_Main_SaveProjectEx(NULL, NSProcessInfo.processInfo.environment[@"REAPER_PTX_TEST_VCA"].UTF8String, 0);
        });
    return 1;
}
