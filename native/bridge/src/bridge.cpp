// FL MCP Bridge — applies the MCP server's note edits to a live FL session.
//
// This is what removes the human from the loop. The piano roll .pyscript that
// preceded it works, but only when a user clicks Tools > Scripting, because FL
// gives piano roll scripts no other entry point. A native effect gets an Idle
// callback instead, so it can pick work up on its own.
//
// It reads the *same* spool the pyscript reads — the pending.json / result.json
// / consumed.json protocol from docs/03 — so the MCP server needed no changes
// to gain an autonomous backend, and the pyscript remains a working fallback
// for anyone who has not installed this. Whichever consumer sees a command
// first records its id in the shared ledger, so it is applied exactly once.
//
// Registered as an EFFECT, not a generator: per fp_plugclass.h, TNotesParams
// .ChanNum retargets to the selected channel only for effects. See
// native/spike1 for the experiment that established this.

#include <unistd.h>

#include <cstddef>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <string>
#include <vector>

#include "fp_cplug.h"
#include "fp_plugclass.h"
#include "minijson.h"

namespace {

// --- load tracing ------------------------------------------------------------
// Without this there is no way to tell "FL never loaded the dylib" from "FL
// loaded it but never instantiated the plugin" from "the plugin ran but could
// not write its heartbeat". Each stage stamps a file in /tmp, which is always
// writable and outside every sandbox, so a failure at any stage is visible to
// the harness instead of presenting as a silent timeout.
void TraceStage(const char* stage) {
  char path[128];
  snprintf(path, sizeof(path), "/tmp/flmcp_trace_%s.txt", stage);
  FILE* f = fopen(path, "w");
  if (f == nullptr) return;
  fprintf(f, "stage=%s\npid=%d\nunix_time=%lld\n", stage, (int)getpid(),
          (long long)time(nullptr));
  fclose(f);
}

// Runs at dlopen, before FL calls anything. Proves the binary was at least
// found and loaded.
__attribute__((constructor)) static void OnDylibLoaded() { TraceStage("dylib_loaded"); }

const int kProtoVersion = 1;
const int kIdleTicksBetweenPolls = 6;   // Idle is ~20ms; poll a few times a sec
const int kMaxConsumedIds = 64;
const int kDefaultPPQ = 96;             // FL's default; overridable per command

// Mirrors mcp-server/src/fl_mcp/flpaths.py. FL allows writes under
// 'Piano roll scripts' recursively but not elsewhere in Settings, so both
// candidates live under it.
const char* kSpoolSubdirs[] = {
    "Piano roll scripts/FLMCP",
    "Piano roll scripts",
};
const int kSpoolSubdirCount = 2;

std::vector<std::string> gSpoolRoots;

void BuildSpoolRoots() {
  gSpoolRoots.clear();
  const char* home = getenv("HOME");
  if (home == nullptr) return;
  for (int i = 0; i < kSpoolSubdirCount; ++i) {
    gSpoolRoots.push_back(std::string(home) +
                          "/Documents/Image-Line/FL Studio/Settings/" +
                          kSpoolSubdirs[i]);
  }
}

std::string Join(const std::string& dir, const char* name) {
  return dir + "/" + name;
}

bool ReadFile(const std::string& path, std::string* out) {
  FILE* f = fopen(path.c_str(), "rb");
  if (f == nullptr) return false;
  out->clear();
  char buffer[4096];
  size_t n;
  while ((n = fread(buffer, 1, sizeof(buffer), f)) > 0) out->append(buffer, n);
  fclose(f);
  return true;
}

bool WriteFile(const std::string& path, const std::string& text) {
  FILE* f = fopen(path.c_str(), "wb");
  if (f == nullptr) return false;
  size_t written = fwrite(text.data(), 1, text.size(), f);
  fclose(f);
  return written == text.size();
}

// --- consumed-id ledger ------------------------------------------------------
// Shared with the .pyscript. Consumption cannot depend on deleting or
// overwriting pending.json: the pyscript runs in FL's sandbox where os.remove
// is blocked, and the folder holding a command may be readable but not
// writable. The ledger is the single source of truth for "already applied".

std::vector<int> ReadConsumedIds() {
  std::vector<int> ids;
  for (const std::string& root : gSpoolRoots) {
    std::string text;
    if (!ReadFile(Join(root, "consumed.json"), &text)) continue;
    minijson::Value doc;
    if (!minijson::Parse(text.c_str(), &doc)) continue;
    const minijson::Value* list = doc.Get("ids");
    if (list == nullptr || list->type != minijson::Type::Array) continue;
    for (const minijson::Value& item : list->array) {
      if (item.type != minijson::Type::Number) continue;
      int id = static_cast<int>(item.number);
      bool seen = false;
      for (int existing : ids) seen = seen || existing == id;
      if (!seen) ids.push_back(id);
    }
  }
  return ids;
}

bool AlreadyConsumed(int id) {
  for (int existing : ReadConsumedIds()) {
    if (existing == id) return true;
  }
  return false;
}

// Returns how many roots accepted the write. Zero means the command will be
// re-applied on the next poll, which the caller must report rather than hide.
int RecordConsumed(int id) {
  std::vector<int> ids = ReadConsumedIds();
  bool seen = false;
  for (int existing : ids) seen = seen || existing == id;
  if (!seen) ids.push_back(id);
  if ((int)ids.size() > kMaxConsumedIds) {
    ids.erase(ids.begin(), ids.begin() + (ids.size() - kMaxConsumedIds));
  }

  std::string json = "{\"ids\": [";
  for (size_t i = 0; i < ids.size(); ++i) {
    if (i != 0) json += ", ";
    json += std::to_string(ids[i]);
  }
  json += "]}";

  std::string tombstone =
      "{\"consumed\": true, \"id\": " + std::to_string(id) + "}";

  int accepted = 0;
  for (const std::string& root : gSpoolRoots) {
    if (WriteFile(Join(root, "consumed.json"), json)) ++accepted;
    std::string pending = Join(root, "pending.json");
    std::string existing;
    if (ReadFile(pending, &existing)) WriteFile(pending, tombstone);
  }
  return accepted;
}

void WriteResult(const std::string& json) {
  for (const std::string& root : gSpoolRoots) {
    WriteFile(Join(root, "result.json"), json);
  }
}

void WriteHeartbeat(long long tick) {
  char buffer[256];
  snprintf(buffer, sizeof(buffer),
           "tick=%lld\npid=%d\nunix_time=%lld\nbackend=bridge\n", tick,
           (int)getpid(), (long long)time(nullptr));
  for (const std::string& root : gSpoolRoots) {
    WriteFile(Join(root, "bridge_heartbeat.txt"), buffer);
  }
}

char gLongName[] = "FL MCP Bridge";
char gShortName[] = "MCPBR";

// Name pointers are assigned at runtime: fp_plugclass.h uses #pragma pack(4),
// putting LongName at offset 4, and arm64 cannot emit a pointer relocation at a
// 4-byte-aligned address. Static initialisation fails to link.
TFruityPlugInfo gPlugInfo = {
    CurrentSDKVersion,
    nullptr,
    nullptr,
    FPF_Type_Effect,  // effect, not generator — this is what enables ChanNum
    0,                // NumParams
    0,                // DefPoly
    0,                // NumOutCtrls
    0,                // NumOutVoices
    {0},
};

void InitPlugInfo() {
  gPlugInfo.LongName = gLongName;
  gPlugInfo.ShortName = gShortName;
}

}  // namespace

class Bridge : public TCPPFruityPlug {
 public:
  Bridge(intptr_t tag, TFruityPlugHost* host);

  void _stdcall DestroyObject() override;
  void _stdcall Idle_Public() override;
  void _stdcall Eff_Render(PWAV32FS source, PWAV32FS dest, int length) override;

 private:
  void Poll();
  void Execute(const minijson::Value& command, const std::string& source_root);
  int AddNotes(const minijson::Value& command, std::string* error);

  int idle_counter_ = 0;
  long long heartbeat_tick_ = 0;
};

Bridge::Bridge(intptr_t tag, TFruityPlugHost* host)
    : TCPPFruityPlug(tag, host, 0) {
  InitPlugInfo();
  Info = &gPlugInfo;
  HostTag = tag;
  PlugHost = host;

  BuildSpoolRoots();

  // Value 2 == always enabled, so Idle keeps firing with no editor open. This
  // plugin has no UI at all, so without it nothing would ever run.
  if (PlugHost != nullptr) {
    PlugHost->Dispatcher(HostTag, FHD_WantIdle, 0, 2);
  }
  TraceStage("ctor_done");
  WriteHeartbeat(0);
}

void _stdcall Bridge::DestroyObject() { delete this; }

void _stdcall Bridge::Eff_Render(PWAV32FS source, PWAV32FS dest, int length) {
  // Sonically inert. Users will leave this on an insert permanently.
  if (source != dest && length > 0) {
    memcpy(dest, source, sizeof(TWAV32FS) * (size_t)length);
  }
}

void _stdcall Bridge::Idle_Public() {
  if (++idle_counter_ < kIdleTicksBetweenPolls) return;
  idle_counter_ = 0;
  WriteHeartbeat(++heartbeat_tick_);
  Poll();
}

void Bridge::Poll() {
  for (const std::string& root : gSpoolRoots) {
    std::string text;
    if (!ReadFile(Join(root, "pending.json"), &text)) continue;

    minijson::Value command;
    if (!minijson::Parse(text.c_str(), &command)) continue;
    if (command.type != minijson::Type::Object) continue;

    // A tombstone left by whichever consumer got here first.
    const minijson::Value* op = command.Get("op");
    if (op == nullptr || op->type != minijson::Type::String) continue;

    Execute(command, root);
    return;  // one command per poll; the next Idle picks up the next
  }
}

void Bridge::Execute(const minijson::Value& command, const std::string& source_root) {
  int id = command.Int("id", 0);
  if (AlreadyConsumed(id)) return;

  int proto = command.Int("proto", 0);
  if (proto != kProtoVersion) {
    RecordConsumed(id);
    WriteResult("{\"id\": " + std::to_string(id) +
                ", \"ok\": false, \"error\": \"protocol mismatch: command is v" +
                std::to_string(proto) + ", bridge is v" +
                std::to_string(kProtoVersion) + "\", \"backend\": \"bridge\"}");
    return;
  }

  // Claim before executing: a command that crashes FL must not replay on
  // restart and crash it again.
  int accepted = RecordConsumed(id);

  std::string op = command.Str("op", "");
  std::string error;
  int written = 0;
  bool ok = false;

  if (op == "add_notes") {
    written = AddNotes(command, &error);
    ok = error.empty();
  } else if (op == "clear_notes") {
    minijson::Value clear;
    clear.type = minijson::Type::Object;
    clear.object.emplace_back("mode", minijson::Value());
    clear.object.back().second.type = minijson::Type::String;
    clear.object.back().second.text = "replace";
    written = AddNotes(clear, &error);
    ok = error.empty();
  } else {
    error = "unknown op: " + op;
  }

  if (PlugHost != nullptr) PlugHost->Dispatcher(HostTag, FHD_SetDirty, 0, 0);

  std::string json = "{\"id\": " + std::to_string(id);
  json += ok ? ", \"ok\": true" : ", \"ok\": false";
  if (!ok) json += ", \"error\": \"" + minijson::Escape(error) + "\"";
  json += ", \"op\": \"" + minijson::Escape(op) + "\"";
  json += ", \"notes_written\": " + std::to_string(written);
  json += ", \"consumed\": " + std::string(accepted > 0 ? "true" : "false");
  json += ", \"backend\": \"bridge\"";
  json += ", \"spool\": \"" + minijson::Escape(source_root) + "\"";
  json += ", \"summary\": \"applied " + std::to_string(written) +
          " note(s) via FHD_AddNotesToPR\"}";
  WriteResult(json);
}

int Bridge::AddNotes(const minijson::Value& command, std::string* error) {
  if (PlugHost == nullptr) {
    *error = "no plugin host";
    return 0;
  }

  const minijson::Value* notes = command.Get("notes");
  int count = 0;
  if (notes != nullptr && notes->type == minijson::Type::Array) {
    count = (int)notes->array.size();
  }

  const double ppq = command.Num("ppq", kDefaultPPQ);
  const bool replace = command.Str("mode", "merge") == "replace";

  // TNoteParams is #pragma pack(1) and TNotesParams ends in a variable-length
  // array, so size from offsetof — sizeof would fold in the trailing element.
  size_t bytes =
      offsetof(TNotesParams, NoteParams) + (size_t)(count > 0 ? count : 1) * sizeof(TNoteParams);
  TNotesParams* params = (TNotesParams*)calloc(1, bytes);
  if (params == nullptr) {
    *error = "out of memory";
    return 0;
  }

  params->Target = 1;  // piano roll
  params->Flags = replace ? NPF_EmptyFirst : 0;
  params->PatNum = command.Int("pattern", -1);
  params->ChanNum = command.Int("channel", -1);
  params->Count = count;

  for (int i = 0; i < count; ++i) {
    const minijson::Value& n = notes->array[(size_t)i];
    TNoteParams* out = &params->NoteParams[i];
    memset(out, 0, sizeof(*out));
    out->Position = (int)(n.Num("start", 0.0) * ppq + 0.5);
    int length = (int)(n.Num("length", 1.0) * ppq + 0.5);
    out->Length = length > 0 ? length : 1;
    out->Note = (short)n.Int("key", 60);
    // The server sends 0..1; FL's Vol default is 100/128.
    out->Vol = (int)(n.Num("velocity", 0.8) * 128.0);
    // Server pan is 0..1 centred at 0.5; TNoteParams.Pan is centred at 0.
    out->Pan = (int)((n.Num("pan", 0.5) - 0.5) * 128.0);
    out->Color = (short)n.Int("color", 0);
    out->Pitch = 0;
    out->FCut = 0.0f;
    out->FRes = 0.0f;
  }

  PlugHost->Dispatcher(HostTag, FHD_AddNotesToPR, 0, (intptr_t)params);
  free(params);
  return count;
}

extern "C" __attribute__((visibility("default"))) TFruityPlug* _stdcall
CreatePlugInstance(TFruityPlugHost* host, int tag) {
  TraceStage("create_called");
  Bridge* plugin = new Bridge(tag, host);
  TraceStage("create_returned");
  return (TFruityPlug*)plugin;
}

extern "C" __attribute__((visibility("default"))) void _stdcall
SetExternalAppHandle(intptr_t handle) {
  (void)handle;
}
