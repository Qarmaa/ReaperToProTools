// vst3host — маленький консольный VST3-хост для переноса настроек плагинов Pro Tools -> Reaper.
// Загружает .vst3, умеет: список параметров, подать состояние (component / controller), задать параметры и получить настоящее состояние плагина.
//   vst3host <bundle.vst3> info
//   vst3host <bundle.vst3> params  [--comp f] [--ctrl f]
//   vst3host <bundle.vst3> state   <out_comp> <out_ctrl> [--comp f] [--ctrl f] [--set id=norm ...]
//   vst3host <bundle.vst3> domain  [--grid N] [id ...]   шкала непрерывных параметров: что плагин показывает в N точках 0..1
//   vst3host <bundle.vst3> buses                          какие форматы каналов (вход = выход) плагин принимает
// Вывод — JSON в stdout, диагностика — в stderr.
#include "public.sdk/source/vst/hosting/module.h"
#include "public.sdk/source/vst/hosting/hostclasses.h"
#include "public.sdk/source/vst/hosting/parameterchanges.h"
#include "public.sdk/source/vst/hosting/processdata.h"
#include "public.sdk/source/common/memorystream.h"
#include "pluginterfaces/vst/ivstcomponent.h"
#include "pluginterfaces/vst/ivsteditcontroller.h"
#include "pluginterfaces/vst/ivstaudioprocessor.h"
#include "pluginterfaces/vst/ivstmessage.h"
#include "pluginterfaces/vst/vsttypes.h"
#include "pluginterfaces/vst/vstspeaker.h"
#include "pluginterfaces/base/ustring.h"
#include "pluginterfaces/base/funknownimpl.h"
#include <cstdio>
#include <unistd.h>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <iterator>
#include <string>
#include <vector>
#include <algorithm>

using namespace Steinberg;
using namespace Steinberg::Vst;

static std::string u16to8 (const TChar* s)
{
	std::string out;
	for (; *s; ++s)
	{
		unsigned c = *s;
		if (c < 0x80) out += static_cast<char> (c);
		else if (c < 0x800) { out += static_cast<char> (0xC0 | (c >> 6)); out += static_cast<char> (0x80 | (c & 63)); }
		else { out += static_cast<char> (0xE0 | (c >> 12)); out += static_cast<char> (0x80 | ((c >> 6) & 63)); out += static_cast<char> (0x80 | (c & 63)); }
	}
	return out;
}

static std::string jesc (const std::string& s)
{
	std::string o;
	for (unsigned char c : s)
	{
		if (c == '"') o += "\\\"";
		else if (c == '\\') o += "\\\\";
		else if (c < 0x20) { char b[8]; snprintf (b, sizeof b, "\\u%04x", c); o += b; }
		else o += static_cast<char> (c);
	}
	return o;
}

static std::string b64 (const char* d, size_t n)
{
	static const char* T = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
	std::string o;
	for (size_t i = 0; i < n; i += 3)
	{
		unsigned v = (unsigned char)d[i] << 16 | (i + 1 < n ? (unsigned char)d[i + 1] << 8 : 0) | (i + 2 < n ? (unsigned char)d[i + 2] : 0);
		o += T[v >> 18 & 63]; o += T[v >> 12 & 63]; o += i + 1 < n ? T[v >> 6 & 63] : '='; o += i + 2 < n ? T[v & 63] : '=';
	}
	return o;
}

static std::vector<char> readFile (const std::string& p)
{
	std::ifstream f (p, std::ios::binary);
	return std::vector<char> ((std::istreambuf_iterator<char> (f)), std::istreambuf_iterator<char> ());
}

static void writeFile (const std::string& p, const char* d, size_t n)
{
	std::ofstream f (p, std::ios::binary);
	f.write (d, static_cast<std::streamsize> (n));
}

// Обработчик событий контроллера: запоминает правки параметров, чтобы передать их процессору
class Handler : public IComponentHandler
{
public:
	std::vector<std::pair<ParamID, ParamValue>> edits;
	tresult PLUGIN_API beginEdit (ParamID) override { return kResultOk; }
	tresult PLUGIN_API performEdit (ParamID id, ParamValue v) override { edits.emplace_back (id, v); return kResultOk; }
	tresult PLUGIN_API endEdit (ParamID) override { return kResultOk; }
	tresult PLUGIN_API restartComponent (int32) override { return kResultOk; }
	tresult PLUGIN_API queryInterface (const TUID _iid, void** obj) override
	{
		if (FUnknownPrivate::iidEqual (_iid, IComponentHandler::iid) || FUnknownPrivate::iidEqual (_iid, FUnknown::iid))
		{ *obj = this; return kResultOk; }
		*obj = nullptr; return kNoInterface;
	}
	uint32 PLUGIN_API addRef () override { return 1000; }
	uint32 PLUGIN_API release () override { return 1000; }
};

struct Plugin
{
	VST3::Hosting::Module::Ptr module;
	IPtr<IComponent> comp;
	IPtr<IEditController> ctrl;
	IPtr<IAudioProcessor> proc;
	bool singleComponent = false;
	Handler handler;
	std::string name, vendor;
};

static HostApplication gHost;

static bool load (const std::string& path, Plugin& p)
{
	std::string err;
	p.module = VST3::Hosting::Module::create (path, err);
	if (!p.module) { fprintf (stderr, "cannot load module: %s\n", err.c_str ()); return false; }
	auto factory = p.module->getFactory ();
	for (auto& ci : factory.classInfos ())
	{
		if (ci.category () != kVstAudioEffectClass) continue;
		p.name = ci.name (); p.vendor = ci.vendor ();
		p.comp = factory.createInstance<IComponent> (ci.ID ());
		if (!p.comp) continue;
		if (p.comp->initialize (&gHost) != kResultOk) { fprintf (stderr, "component init failed\n"); return false; }
		p.proc = U::cast<IAudioProcessor> (p.comp);
		p.ctrl = U::cast<IEditController> (p.comp);
		p.singleComponent = p.ctrl != nullptr;
		if (!p.ctrl)
		{
			TUID cc;
			if (p.comp->getControllerClassId (cc) == kResultOk)
			{
				p.ctrl = factory.createInstance<IEditController> (VST3::UID::fromTUID (cc));
				if (p.ctrl && p.ctrl->initialize (&gHost) != kResultOk) { fprintf (stderr, "controller init failed\n"); return false; }
			}
		}
		if (!p.ctrl) { fprintf (stderr, "no controller\n"); return false; }
		auto cp1 = U::cast<IConnectionPoint> (p.comp);
		auto cp2 = U::cast<IConnectionPoint> (p.ctrl);
		if (cp1 && cp2 && !p.singleComponent) { cp1->connect (cp2); cp2->connect (cp1); }
		p.ctrl->setComponentHandler (&p.handler);
		return true;
	}
	fprintf (stderr, "no audio effect class\n");
	return false;
}

static void setStates (Plugin& p, const std::vector<char>& comp, const std::vector<char>& ctrl, bool hasComp, bool hasCtrl)
{
	if (hasComp)
	{
		MemoryStream s ((void*)comp.data (), (TSize)comp.size ());
		tresult r1 = p.comp->setState (&s);
		MemoryStream s2 ((void*)comp.data (), (TSize)comp.size ());
		tresult r2 = p.ctrl->setComponentState (&s2);
		fprintf (stderr, "component.setState=%d controller.setComponentState=%d\n", (int)r1, (int)r2);
	}
	if (hasCtrl)
	{
		MemoryStream s ((void*)ctrl.data (), (TSize)ctrl.size ());
		tresult r = p.ctrl->setState (&s);
		fprintf (stderr, "controller.setState=%d\n", (int)r);
	}
}

// Прогоняет один короткий блок звука с правками параметров, чтобы изменения дошли до процессора (его состояние берётся из getState компонента)
static void pushToProcessor (Plugin& p, const std::vector<std::pair<ParamID, ParamValue>>& edits)
{
	if (!p.proc || edits.empty ()) return;
	ProcessSetup setup {kRealtime, kSample32, 256, 48000.0};
	p.comp->setActive (false);
	if (p.proc->setupProcessing (setup) != kResultOk) fprintf (stderr, "setupProcessing failed\n");
	for (int dir = 0; dir < 2; ++dir)
		for (int32 i = 0; i < p.comp->getBusCount (kAudio, dir); ++i) p.comp->activateBus (kAudio, dir, i, true);
	p.comp->setActive (true);
	p.proc->setProcessing (true);
	HostProcessData data;
	data.prepare (*p.comp, 256, kSample32);
	ParameterChanges changes ((int32)edits.size ());
	data.inputParameterChanges = &changes;
	data.numSamples = 256;
	for (auto& e : edits)
	{
		int32 qi = 0;
		if (auto* q = changes.addParameterData (e.first, qi)) { int32 pi = 0; q->addPoint (0, e.second, pi); }
	}
	tresult r = p.proc->process (data);
	fprintf (stderr, "process=%d (%zu param changes)\n", (int)r, edits.size ());
	p.proc->setProcessing (false);
}

static void printParams (Plugin& p)
{
	int32 n = p.ctrl->getParameterCount ();
	printf ("{\"name\":\"%s\",\"vendor\":\"%s\",\"single\":%s,\"params\":[", jesc (p.name).c_str (), jesc (p.vendor).c_str (), p.singleComponent ? "true" : "false");
	for (int32 i = 0; i < n; ++i)
	{
		ParameterInfo pi {};
		if (p.ctrl->getParameterInfo (i, pi) != kResultOk) continue;
		ParamValue nv = p.ctrl->getParamNormalized (pi.id);
		String128 str {};
		p.ctrl->getParamStringByValue (pi.id, nv, str);
		printf ("%s{\"id\":%u,\"title\":\"%s\",\"short\":\"%s\",\"units\":\"%s\",\"steps\":%d,\"default\":%.9g,\"flags\":%d,\"norm\":%.9g,\"str\":\"%s\"}",
				i ? "," : "", pi.id, jesc (u16to8 (pi.title)).c_str (), jesc (u16to8 (pi.shortTitle)).c_str (), jesc (u16to8 (pi.units)).c_str (),
				(int)pi.stepCount, pi.defaultNormalizedValue, (int)pi.flags, nv, jesc (u16to8 (str)).c_str ());
	}
	printf ("]}\n");
}

// Шкала параметра: [[норм. значение, строка плагина], ...] на сетке из n точек (только непрерывные параметры)
static void printDomain (Plugin& p, int n, const std::vector<ParamID>& only)
{
	printf ("{");
	bool first = true;
	for (int32 i = 0; i < p.ctrl->getParameterCount (); ++i)
	{
		ParameterInfo pi {};
		if (p.ctrl->getParameterInfo (i, pi) != kResultOk || pi.stepCount != 0) continue;
		if (!only.empty () && std::find (only.begin (), only.end (), pi.id) == only.end ()) continue;
		printf ("%s\"%u\":[", first ? "" : ",", pi.id);
		first = false;
		ParamValue keep = p.ctrl->getParamNormalized (pi.id);
		for (int k = 0; k < n; ++k)
		{
			ParamValue v = n > 1 ? (ParamValue)k / (n - 1) : 0.0;
			String128 str {};
			p.ctrl->setParamNormalized (pi.id, v);                  // часть плагинов (JUCE) подписывает только текущее значение, а не переданное
			p.ctrl->getParamStringByValue (pi.id, p.ctrl->getParamNormalized (pi.id), str);
			printf ("%s[%.9g,\"%s\"]", k ? "," : "", v, jesc (u16to8 (str)).c_str ());
		}
		p.ctrl->setParamNormalized (pi.id, keep);
		printf ("]");
	}
	printf ("}\n");
}

// Форматы каналов: главный вход и главный выход в одном формате; остальные шины (side-chain) — как есть
static void printBuses (Plugin& p)
{
	const std::pair<const char*, SpeakerArrangement> arrs[] = {{"mono", SpeakerArr::kMono}, {"stereo", SpeakerArr::kStereo}, {"LCR", SpeakerArr::k30Cine},
		{"quad", SpeakerArr::k40Music}, {"LCRS", SpeakerArr::k40Cine}, {"5.0", SpeakerArr::k50}, {"5.1", SpeakerArr::k51}, {"7.1", SpeakerArr::k71Cine}};
	auto proc = p.proc;
	printf ("{");
	bool first = true;
	for (auto& a : arrs)
	{
		bool ok = false;
		if (proc)
		{
			std::vector<SpeakerArrangement> in (std::max (1, (int)p.comp->getBusCount (kAudio, kInput))), out (std::max (1, (int)p.comp->getBusCount (kAudio, kOutput)));
			for (size_t i = 1; i < in.size (); ++i) proc->getBusArrangement (kInput, (int32)i, in[i]);
			for (size_t i = 1; i < out.size (); ++i) proc->getBusArrangement (kOutput, (int32)i, out[i]);
			in[0] = out[0] = a.second;
			int32 nin = p.comp->getBusCount (kAudio, kInput);
			tresult r = proc->setBusArrangements (nin ? in.data () : nullptr, nin, out.data (), (int32)out.size ());
			if (r != kResultTrue && in.size () > 1)
			{
				for (auto& x : in) x = a.second;                       // side-chain в том же формате (так требует, например, Pro-Q 3)
				r = proc->setBusArrangements (in.data (), nin, out.data (), (int32)out.size ());
			}
			if (r == kResultTrue)
			{
				SpeakerArrangement gi = 0, go = 0;
				proc->getBusArrangement (kOutput, 0, go);
				if (nin) proc->getBusArrangement (kInput, 0, gi); else gi = a.second;
				ok = gi == a.second && go == a.second;          // плагин может «согласиться», но выставить другой формат
			}
		}
		printf ("%s\"%s\":%s", first ? "" : ",", a.first, ok ? "true" : "false");
		first = false;
	}
	printf ("}\n");
}

int main (int argc, char** argv)
{
	if (argc < 3) { fprintf (stderr, "usage: vst3host <bundle.vst3> info|params|state ...\n"); return 2; }
	std::string path = argv[1], cmd = argv[2];
	Plugin p;
	if (!load (path, p)) return 1;
	std::vector<char> comp, ctrl; bool hasComp = false, hasCtrl = false;
	std::vector<std::pair<ParamID, ParamValue>> sets;
	std::string outComp, outCtrl;
	int grid = 101; std::vector<ParamID> only;
	for (int i = 3; i < argc; ++i)
	{
		std::string a = argv[i];
		if (a == "--comp" && i + 1 < argc) { comp = readFile (argv[++i]); hasComp = true; }
		else if (a == "--ctrl" && i + 1 < argc) { ctrl = readFile (argv[++i]); hasCtrl = true; }
		else if (a == "--set" && i + 1 < argc)
		{
			std::string kv = argv[++i]; auto eq = kv.find ('=');
			if (eq != std::string::npos) sets.emplace_back ((ParamID)std::stoul (kv.substr (0, eq)), std::stod (kv.substr (eq + 1)));
		}
		else if (a == "--grid" && i + 1 < argc) grid = std::max (2, std::min (4097, std::atoi (argv[++i])));
		else if (cmd == "domain") only.push_back ((ParamID)std::stoul (a));
		else if (cmd == "state" && outComp.empty ()) outComp = a;
		else if (cmd == "state" && outCtrl.empty ()) outCtrl = a;
	}
	if (cmd == "info")
	{
		printf ("{\"name\":\"%s\",\"vendor\":\"%s\",\"single\":%s,\"paramCount\":%d}\n", jesc (p.name).c_str (), jesc (p.vendor).c_str (), p.singleComponent ? "true" : "false", p.ctrl->getParameterCount ());
		fflush (stdout);
		_exit (0);
	}
	if (cmd == "buses") { printBuses (p); fflush (stdout); _exit (0); }
	setStates (p, comp, ctrl, hasComp, hasCtrl);
	if (cmd == "domain") { printDomain (p, grid, only); fflush (stdout); _exit (0); }
	for (auto& s : sets) { p.ctrl->setParamNormalized (s.first, s.second); }
	if (!sets.empty ()) pushToProcessor (p, sets);
	if (cmd == "params") { printParams (p); fflush (stdout); _exit (0); }
	if (cmd == "batch")
	{
		// stdin: по строке на экземпляр: «id=norm id=norm ...» (пустая строка — состояние по умолчанию). Вывод: JSON-строка {"comp":"b64","ctrl":"b64"}
		MemoryStream defC, defX;
		p.comp->getState (&defC);
		if (p.singleComponent) p.ctrl->getState (&defX); else p.ctrl->getState (&defX);
		std::string line;
		while (std::getline (std::cin, line))
		{
			std::vector<std::pair<ParamID, ParamValue>> ed;
			size_t pos = 0;
			while (pos < line.size ())
			{
				size_t sp = line.find (' ', pos); if (sp == std::string::npos) sp = line.size ();
				std::string kv = line.substr (pos, sp - pos); auto eq = kv.find ('=');
				if (eq != std::string::npos)
				{
					std::string ks = kv.substr (0, eq); bool plain = !ks.empty () && ks.back () == 'p';   // «idp=значение» — значение в реальных единицах плагина
					if (plain) ks.pop_back ();
					ParamID pid = (ParamID)std::stoul (ks); double v = std::stod (kv.substr (eq + 1));
					if (plain) v = p.ctrl->plainParamToNormalized (pid, v);
					ed.emplace_back (pid, v);
				}
				pos = sp + 1;
			}
			{ MemoryStream a (defC.getData (), defC.getSize ()); p.comp->setState (&a); MemoryStream a2 (defC.getData (), defC.getSize ()); p.ctrl->setComponentState (&a2); }
			for (auto& s2 : ed) p.ctrl->setParamNormalized (s2.first, s2.second);
			pushToProcessor (p, ed);
			MemoryStream sc, st;
			p.comp->getState (&sc); p.ctrl->getState (&st);
			printf ("{\"comp\":\"%s\",\"ctrl\":\"%s\"}\n", b64 (sc.getData (), (size_t)sc.getSize ()).c_str (), b64 (st.getData (), (size_t)st.getSize ()).c_str ());
			fflush (stdout);
		}
		_exit (0);
	}
	if (cmd == "state")
	{
		MemoryStream sc, st;
		tresult r1 = p.comp->getState (&sc);
		tresult r2 = p.singleComponent ? kResultOk : p.ctrl->getState (&st);
		if (p.singleComponent) p.ctrl->getState (&st);
		fprintf (stderr, "getState comp=%d (%lld bytes) ctrl=%d (%lld bytes)\n", (int)r1, (long long)sc.getSize (), (int)r2, (long long)st.getSize ());
		if (!outComp.empty ()) writeFile (outComp, sc.getData (), (size_t)sc.getSize ());
		if (!outCtrl.empty ()) writeFile (outCtrl, st.getData (), (size_t)st.getSize ());
		printf ("{\"comp\":%lld,\"ctrl\":%lld}\n", (long long)sc.getSize (), (long long)st.getSize ());
		fflush (stdout);
		_exit (0);
	}
	return 2;
}
