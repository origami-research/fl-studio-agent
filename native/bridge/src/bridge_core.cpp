#include "bridge_core.h"

#include <sys/stat.h>

#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <cstring>

#include "fp_plugclass.h"

namespace bridge_core {

namespace {

std::string Join(const std::string &dir, const char *name) { return dir + "/" + name; }

// The server's zero-padded per-command file name (spool.py f"{id:06d}.json").
std::string CommandName(int id) {
  char name[32];
  snprintf(name, sizeof(name), "%06d.json", id);
  return name;
}

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

// Pending ids listed by any root's queue index. The queue - never a directory
// listing - is the discovery mechanism, matching the pyscript (directory
// scanning is unmeasured in FL's sandbox; here it is just protocol fidelity).
bool ReadQueue(const std::string &root, std::vector<int> *out) {
  std::string text;
  if (!ReadFile(Join(root, "commands/queue.json"), &text)) return false;
  minijson::Value doc;
  if (!minijson::Parse(text.c_str(), &doc)) return false;
  const minijson::Value *list = doc.Get("ids");
  if (list == nullptr || list->type != minijson::Type::Array) return false;
  for (const minijson::Value &item : list->array) {
    if (item.type == minijson::Type::Number) out->push_back(static_cast<int>(item.number));
  }
  return true;
}

// First readable non-tombstone copy of the command across roots (the mirrored
// copies are equivalent; a tombstone anywhere means settled only when no root
// still holds the real command - same rule as the pyscript's _read_command).
bool ReadCommand(const std::vector<std::string> &roots, int id, minijson::Value *out) {
  for (const std::string &root : roots) {
    std::string text;
    if (!ReadFile(Join(root, ("commands/" + CommandName(id)).c_str()), &text)) continue;
    minijson::Value parsed;
    if (!minijson::Parse(text.c_str(), &parsed)) continue;
    if (parsed.type != minijson::Type::Object) continue;
    const minijson::Value *op = parsed.Get("op");
    if (op == nullptr || op->type != minijson::Type::String) continue;  // tombstone
    *out = parsed;
    return true;
  }
  return false;
}

void WriteResponse(const std::vector<std::string> &roots, int id, const std::string &json) {
  for (const std::string &root : roots) {
    // The server mkdirs responses/ at Spool construction; recreate it here so a
    // half-provisioned root still gets its mirror copy (native code, no sandbox).
    mkdir(Join(root, "responses").c_str(), 0755);
    WriteFile(Join(root, ("responses/" + CommandName(id)).c_str()), json);
  }
}

bool Contains(const std::vector<int> &ids, int id) {
  return std::find(ids.begin(), ids.end(), id) != ids.end();
}

// SpoolResponse.op is a closed literal in schemas.py; an unexpected string
// would fail the server's validation, so anything else is reported as op-less.
bool IsKnownOp(const std::string &op) {
  return op == "add_notes" || op == "clear_notes" || op == "ping";
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
    : params(other.params),
      bytes(other.bytes),
      count(other.count),
      ppq(other.ppq),
      used_default_ppq(other.used_default_ppq) {
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
    ppq = other.ppq;
    used_default_ppq = other.used_default_ppq;
    other.params = nullptr;
    other.bytes = 0;
    other.count = 0;
  }
  return *this;
}

NotesParamsBuffer::~NotesParamsBuffer() { free(params); }

NotesParamsBuffer BuildNotesParams(const minijson::Value &args, int ppq_default, bool empty_first) {
  NotesParamsBuffer buf;

  const minijson::Value *notes = args.Get("notes");
  int count = 0;
  if (notes != nullptr && notes->type == minijson::Type::Array) {
    count = static_cast<int>(notes->array.size());
  }
  if (count > kMaxNotesPerCommand) count = kMaxNotesPerCommand;

  // The v2 ppq stamp (schemas.AddNotesArgs.ppq). None serializes as JSON null,
  // so anything but a positive number falls back - flagged, so the response
  // summary can carry the warning.
  const minijson::Value *ppq_field = args.Get("ppq");
  buf.used_default_ppq =
      ppq_field == nullptr || ppq_field->type != minijson::Type::Number || ppq_field->number <= 0;
  const double ppq = buf.used_default_ppq ? static_cast<double>(ppq_default) : ppq_field->number;
  buf.ppq = static_cast<int>(ppq);

  const bool replace = empty_first || args.Str("mode", "merge") == "replace";
  const minijson::Value *target = args.Get("target");
  const bool has_target = target != nullptr && target->type == minijson::Type::Object;

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
  params->PatNum = has_target ? target->Int("pattern", -1) : -1;   // -1 = current pattern
  params->ChanNum = has_target ? target->Int("channel", -1) : -1;  // -1 = selected (effect only)
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

// --- queue + consumed-id ledger ----------------------------------------------

std::vector<int> QueueIds(const std::vector<std::string> &roots) {
  std::vector<int> ids;
  for (const std::string &root : roots) {
    std::vector<int> from_root;
    if (!ReadQueue(root, &from_root)) continue;
    for (int id : from_root) {
      if (!Contains(ids, id)) ids.push_back(id);
    }
  }
  std::sort(ids.begin(), ids.end());
  return ids;
}

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
      if (!Contains(ids, id)) ids.push_back(id);
    }
  }
  return ids;
}

bool AlreadyConsumed(const std::vector<std::string> &roots, int id) {
  return Contains(ReadConsumedIds(roots), id);
}

int RecordConsumed(const std::vector<std::string> &roots, int id) {
  std::vector<int> ids = ReadConsumedIds(roots);
  if (!Contains(ids, id)) ids.push_back(id);
  if (static_cast<int>(ids.size()) > kMaxConsumedIds) {
    ids.erase(ids.begin(), ids.begin() + (ids.size() - kMaxConsumedIds));
  }

  std::string json = "{\"ids\": [";
  for (size_t i = 0; i < ids.size(); ++i) {
    if (i != 0) json += ", ";
    json += std::to_string(ids[i]);
  }
  json += "]}";

  // Same tombstone the server writes on timeout and the pyscript writes on
  // consume; the ledger, not the tombstone, is what correctness depends on
  // (a root may be readable but not writable).
  std::string tombstone = "{\"consumed\": true, \"id\": " + std::to_string(id) + "}";

  int accepted = 0;
  for (const std::string &root : roots) {
    if (WriteFile(Join(root, "consumed.json"), json)) ++accepted;
    std::string command = Join(root, ("commands/" + CommandName(id)).c_str());
    std::string existing;
    if (ReadFile(command, &existing)) WriteFile(command, tombstone);
  }
  return accepted;
}

// --- one poll cycle ----------------------------------------------------------

PollOutcome PollOnce(TFruityPlugHost *host, std::intptr_t host_tag,
                     const std::vector<std::string> &roots, int ppq_default) {
  PollOutcome outcome;

  // Lowest fresh id first: the server allocates ids in send order, and the
  // pyscript applies in the same order, so both consumers agree on sequencing.
  std::vector<int> pending = QueueIds(roots);
  if (pending.empty()) return outcome;
  std::vector<int> consumed = ReadConsumedIds(roots);

  minijson::Value command;
  int id = 0;
  bool found = false;
  for (int candidate : pending) {
    if (Contains(consumed, candidate)) continue;
    if (ReadCommand(roots, candidate, &command)) {
      id = candidate;
      found = true;
      break;
    }
    // Tombstoned everywhere (server-cancelled) or unreadable: skip, never
    // block the ids queued behind it.
  }
  if (!found) return outcome;

  outcome.had_command = true;
  outcome.id = id;
  outcome.op = command.Str("op", "");

  // Claim before executing: a command that crashes FL must not replay on
  // restart and crash it again. Same ordering as the pyscript (_consume_all
  // runs before the proto check and the handler).
  RecordConsumed(roots, id);

  std::string summary;
  const int proto = command.Int("proto", 0);
  if (proto != kProtoVersion) {
    outcome.error = "protocol mismatch: command is v" + std::to_string(proto) + ", bridge is v" +
                    std::to_string(kProtoVersion);
  } else if (outcome.op == "ping") {
    outcome.ok = true;
    summary = "pong";
  } else if (outcome.op == "add_notes" || outcome.op == "clear_notes") {
    const minijson::Value *args = command.Get("args");
    minijson::Value no_args;
    no_args.type = minijson::Type::Object;
    const minijson::Value &checked_args =
        (args != nullptr && args->type == minijson::Type::Object) ? *args : no_args;
    if (host == nullptr) {
      outcome.error = "no plugin host";
    } else {
      NotesParamsBuffer buf =
          BuildNotesParams(checked_args, ppq_default, outcome.op == "clear_notes");
      if (buf.params == nullptr) {
        outcome.error = "out of memory building notes";
      } else {
        host->Dispatcher(host_tag, FHD_AddNotesToPR, 0, reinterpret_cast<intptr_t>(buf.params));
        host->Dispatcher(host_tag, FHD_SetDirty, 0, 0);
        outcome.notes_written = buf.count;
        outcome.ok = true;
        summary = "applied " + std::to_string(buf.count) + " note(s) via FHD_AddNotesToPR at ppq " +
                  std::to_string(buf.ppq);
        if (buf.used_default_ppq && outcome.op == "add_notes") {
          summary += " (WARNING: command carried no ppq stamp; assumed default " +
                     std::to_string(ppq_default) + " - horizontal placement unverified)";
        }
      }
    }
  } else {
    outcome.error = "unknown op: " + outcome.op;
  }

  // The v2 SpoolResponse shape ({id, ok, op, notes_written, err}) plus
  // diagnostic extras (backend, summary) that pydantic ignores on validate.
  // notes_written is this consumer's applied count - an ECHO, not a readback
  // (FHD_AddNotesToPR returns nothing); the session's receipts must not (and
  // do not) trust it.
  std::string json = "{\"id\": " + std::to_string(id);
  json += outcome.ok ? ", \"ok\": true" : ", \"ok\": false";
  if (IsKnownOp(outcome.op)) json += ", \"op\": \"" + outcome.op + "\"";
  if (outcome.ok) json += ", \"notes_written\": " + std::to_string(outcome.notes_written);
  if (!outcome.ok) json += ", \"err\": \"" + minijson::Escape(outcome.error) + "\"";
  json += ", \"backend\": \"bridge\"";
  if (!summary.empty()) json += ", \"summary\": \"" + minijson::Escape(summary) + "\"";
  json += "}";
  WriteResponse(roots, id, json);
  return outcome;
}

}  // namespace bridge_core
