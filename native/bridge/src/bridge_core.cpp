#include "bridge_core.h"

#include <cstdio>
#include <cstdlib>
#include <cstring>

#include "fp_plugclass.h"

namespace bridge_core {

namespace {

std::string Join(const std::string &dir, const char *name) { return dir + "/" + name; }

bool ReadFile(const std::string &path, std::string *out) {
  FILE *f = fopen(path.c_str(), "rb");
  if (f == nullptr) return false;
  out->clear();
  char buffer[4096];
  size_t n;
  while ((n = fread(buffer, 1, sizeof(buffer), f)) > 0) out->append(buffer, n);
  fclose(f);
  return true;
}

bool WriteFile(const std::string &path, const std::string &text) {
  FILE *f = fopen(path.c_str(), "wb");
  if (f == nullptr) return false;
  size_t written = fwrite(text.data(), 1, text.size(), f);
  fclose(f);
  return written == text.size();
}

void WriteResult(const std::vector<std::string> &roots, const std::string &json) {
  for (const std::string &root : roots) WriteFile(Join(root, "result.json"), json);
}

}  // namespace

std::vector<std::string> SpoolRoots(const char *home) {
  std::vector<std::string> roots;
  if (home == nullptr || home[0] == '\0') return roots;
  const char *subdirs[] = {"Piano roll scripts/FLMCP", "Piano roll scripts"};
  for (const char *sub : subdirs) {
    roots.push_back(std::string(home) + "/Documents/Image-Line/FL Studio/Settings/" + sub);
  }
  return roots;
}

// --- NotesParamsBuffer -------------------------------------------------------

NotesParamsBuffer::NotesParamsBuffer(NotesParamsBuffer &&other) noexcept
    : params(other.params), bytes(other.bytes), count(other.count) {
  other.params = nullptr;
  other.bytes = 0;
  other.count = 0;
}

NotesParamsBuffer &NotesParamsBuffer::operator=(NotesParamsBuffer &&other) noexcept {
  if (this != &other) {
    free(params);
    params = other.params;
    bytes = other.bytes;
    count = other.count;
    other.params = nullptr;
    other.bytes = 0;
    other.count = 0;
  }
  return *this;
}

NotesParamsBuffer::~NotesParamsBuffer() { free(params); }

NotesParamsBuffer BuildNotesParams(const minijson::Value &command, int ppq_default) {
  NotesParamsBuffer buf;

  const minijson::Value *notes = command.Get("notes");
  int count = 0;
  if (notes != nullptr && notes->type == minijson::Type::Array) {
    count = static_cast<int>(notes->array.size());
  }
  if (count < 0) count = 0;
  if (count > kMaxNotesPerCommand) count = kMaxNotesPerCommand;

  const double ppq = command.Num("ppq", static_cast<double>(ppq_default));
  const bool replace = command.Str("mode", "merge") == "replace";

  // TNoteParams is #pragma pack(1) and TNotesParams ends in a flexible array,
  // so size from offsetof, not sizeof (which would fold in the trailing
  // element). Always allocate at least one slot so a pure clear (count 0 +
  // replace) still has a valid, if unused, NoteParams[0].
  const int alloc_slots = count > 0 ? count : 1;
  size_t bytes = offsetof(TNotesParams, NoteParams) + static_cast<size_t>(alloc_slots) * sizeof(TNoteParams);
  TNotesParams *params = static_cast<TNotesParams *>(calloc(1, bytes));
  if (params == nullptr) return buf;  // count stays 0; caller reports out-of-memory

  params->Target = 1;  // piano roll
  params->Flags = replace ? NPF_EmptyFirst : 0;
  params->PatNum = command.Int("pattern", -1);   // -1 = current pattern
  params->ChanNum = command.Int("channel", -1);  // -1 = selected channel (effect only)
  params->Count = count;

  for (int i = 0; i < count; ++i) {
    const minijson::Value &n = notes->array[static_cast<size_t>(i)];
    TNoteParams *out = &params->NoteParams[i];
    // calloc already zeroed the struct.
    out->Position = static_cast<int>(n.Num("start", 0.0) * ppq + 0.5);
    int length = static_cast<int>(n.Num("length", 1.0) * ppq + 0.5);
    out->Length = length > 0 ? length : 1;
    out->Note = static_cast<short>(n.Int("key", 60));
    // Server sends velocity 0..1; FL's Vol default is 100/128.
    int vol = static_cast<int>(n.Num("velocity", 0.8) * 128.0 + 0.5);
    out->Vol = vol < 0 ? 0 : (vol > 128 ? 128 : vol);
    // Server pan is 0..1 centred at 0.5; TNoteParams.Pan is centred at 0.
    double pan = (n.Num("pan", 0.5) - 0.5) * 128.0;
    out->Pan = static_cast<int>(pan + (pan >= 0 ? 0.5 : -0.5));
    out->Color = static_cast<short>(n.Int("color", 0));
    out->Pitch = 0;
    out->FCut = 0.0f;
    out->FRes = 0.0f;
  }

  buf.params = params;
  buf.bytes = bytes;
  buf.count = count;
  return buf;
}

// --- consumed-id ledger ------------------------------------------------------

std::vector<int> ReadConsumedIds(const std::vector<std::string> &roots) {
  std::vector<int> ids;
  for (const std::string &root : roots) {
    std::string text;
    if (!ReadFile(Join(root, "consumed.json"), &text)) continue;
    minijson::Value doc;
    if (!minijson::Parse(text.c_str(), &doc)) continue;
    const minijson::Value *list = doc.Get("ids");
    if (list == nullptr || list->type != minijson::Type::Array) continue;
    for (const minijson::Value &item : list->array) {
      if (item.type != minijson::Type::Number) continue;
      int id = static_cast<int>(item.number);
      bool seen = false;
      for (int existing : ids) seen = seen || existing == id;
      if (!seen) ids.push_back(id);
    }
  }
  return ids;
}

bool AlreadyConsumed(const std::vector<std::string> &roots, int id) {
  for (int existing : ReadConsumedIds(roots)) {
    if (existing == id) return true;
  }
  return false;
}

int RecordConsumed(const std::vector<std::string> &roots, int id) {
  std::vector<int> ids = ReadConsumedIds(roots);
  bool seen = false;
  for (int existing : ids) seen = seen || existing == id;
  if (!seen) ids.push_back(id);
  if (static_cast<int>(ids.size()) > kMaxConsumedIds) {
    ids.erase(ids.begin(), ids.begin() + (ids.size() - kMaxConsumedIds));
  }

  std::string json = "{\"ids\": [";
  for (size_t i = 0; i < ids.size(); ++i) {
    if (i != 0) json += ", ";
    json += std::to_string(ids[i]);
  }
  json += "]}";

  std::string tombstone = "{\"consumed\": true, \"id\": " + std::to_string(id) + "}";

  int accepted = 0;
  for (const std::string &root : roots) {
    if (WriteFile(Join(root, "consumed.json"), json)) ++accepted;
    // Overwrite pending.json with a tombstone where writable; the ledger, not
    // this, is authoritative (a root may be readable but not writable).
    std::string pending = Join(root, "pending.json");
    std::string existing;
    if (ReadFile(pending, &existing)) WriteFile(pending, tombstone);
  }
  return accepted;
}

// --- one poll cycle ----------------------------------------------------------

PollOutcome PollOnce(TFruityPlugHost *host, std::intptr_t host_tag,
                     const std::vector<std::string> &roots, int ppq_default) {
  PollOutcome outcome;

  // Find the newest readable pending command across roots.
  const minijson::Value *chosen = nullptr;
  minijson::Value command;
  std::string source_root;
  for (const std::string &root : roots) {
    std::string text;
    if (!ReadFile(Join(root, "pending.json"), &text)) continue;
    minijson::Value parsed;
    if (!minijson::Parse(text.c_str(), &parsed)) continue;
    if (parsed.type != minijson::Type::Object) continue;
    const minijson::Value *op = parsed.Get("op");
    if (op == nullptr || op->type != minijson::Type::String) continue;  // tombstone or malformed
    command = parsed;
    chosen = &command;
    source_root = root;
    break;  // preferred root first; one command per poll
  }
  if (chosen == nullptr) return outcome;  // nothing to do

  int id = command.Int("id", 0);
  if (AlreadyConsumed(roots, id)) return outcome;  // another consumer already applied it

  outcome.had_command = true;
  outcome.id = id;
  outcome.op = command.Str("op", "");

  int proto = command.Int("proto", 0);
  if (proto != kProtoVersion) {
    RecordConsumed(roots, id);
    outcome.error = "protocol mismatch: command is v" + std::to_string(proto) + ", bridge is v" +
                    std::to_string(kProtoVersion);
    WriteResult(roots, "{\"id\": " + std::to_string(id) + ", \"ok\": false, \"error\": \"" +
                           minijson::Escape(outcome.error) + "\", \"backend\": \"bridge\"}");
    return outcome;
  }

  // Claim before executing: a command that crashes FL must not replay on
  // restart and crash it again.
  int accepted = RecordConsumed(roots, id);

  const std::string op = outcome.op;
  if (op == "add_notes" || op == "clear_notes") {
    minijson::Value clear_cmd;
    const minijson::Value *apply_cmd = chosen;
    if (op == "clear_notes") {
      // A clear is an add of zero notes with replace mode.
      clear_cmd.type = minijson::Type::Object;
      clear_cmd.object.emplace_back("mode", minijson::Value());
      clear_cmd.object.back().second.type = minijson::Type::String;
      clear_cmd.object.back().second.text = "replace";
      // carry channel/pattern through if present
      if (const minijson::Value *ch = command.Get("channel")) clear_cmd.object.emplace_back("channel", *ch);
      if (const minijson::Value *pt = command.Get("pattern")) clear_cmd.object.emplace_back("pattern", *pt);
      apply_cmd = &clear_cmd;
    }
    if (host == nullptr) {
      outcome.error = "no plugin host";
    } else {
      NotesParamsBuffer buf = BuildNotesParams(*apply_cmd, ppq_default);
      if (buf.params == nullptr) {
        outcome.error = "out of memory building notes";
      } else {
        host->Dispatcher(host_tag, FHD_AddNotesToPR, 0, reinterpret_cast<intptr_t>(buf.params));
        host->Dispatcher(host_tag, FHD_SetDirty, 0, 0);
        outcome.notes_written = buf.count;
        outcome.ok = true;
      }
    }
  } else {
    outcome.error = "unknown op: " + op;
  }

  std::string json = "{\"id\": " + std::to_string(id);
  json += outcome.ok ? ", \"ok\": true" : ", \"ok\": false";
  if (!outcome.ok) json += ", \"error\": \"" + minijson::Escape(outcome.error) + "\"";
  json += ", \"op\": \"" + minijson::Escape(op) + "\"";
  json += ", \"notes_written\": " + std::to_string(outcome.notes_written);
  json += ", \"consumed\": " + std::string(accepted > 0 ? "true" : "false");
  json += ", \"backend\": \"bridge\"";
  json += ", \"spool\": \"" + minijson::Escape(source_root) + "\"";
  json += ", \"summary\": \"applied " + std::to_string(outcome.notes_written) +
          " note(s) via FHD_AddNotesToPR\"}";
  WriteResult(roots, json);
  return outcome;
}

}  // namespace bridge_core
