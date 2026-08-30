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

// --- transport seam ----------------------------------------------------------

void CommandQueue::Push(InboundDatagram datagram) {
  std::lock_guard<std::mutex> lock(mu_);
  if (items_.size() >= capacity_) {
    items_.pop_front();
    ++dropped_;
  }
  items_.push_back(std::move(datagram));
}

std::vector<InboundDatagram> CommandQueue::Drain(std::size_t max_items) {
  std::vector<InboundDatagram> out;
  std::lock_guard<std::mutex> lock(mu_);
  while (!items_.empty() && out.size() < max_items) {
    out.push_back(std::move(items_.front()));
    items_.pop_front();
  }
  return out;
}

std::uint64_t CommandQueue::dropped() const {
  std::lock_guard<std::mutex> lock(mu_);
  return dropped_;
}

const std::string *ResultCache::Find(int id) const {
  for (const auto &entry : entries_) {
    if (entry.first == id) return &entry.second;
  }
  return nullptr;
}

void ResultCache::Put(int id, std::string ack_json) {
  if (entries_.size() >= capacity_) entries_.pop_front();
  entries_.emplace_back(id, std::move(ack_json));
}

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

  // Ledger only. Mailbox disposal (tombstoning pending.json) is the file
  // carrier's job in PollOnce: a UDP-carried command must never clobber an
  // unrelated command sitting in the file mailbox.
  int accepted = 0;
  for (const std::string &root : roots) {
    if (WriteFile(Join(root, "consumed.json"), json)) ++accepted;
  }
  return accepted;
}

// --- the executor -------------------------------------------------------------

ExecResult ExecuteCommand(TFruityPlugHost *host, std::intptr_t host_tag,
                          const std::vector<std::string> &roots, int ppq_default,
                          const std::string &payload_json, const std::string &carrier,
                          ResultCache *cache) {
  ExecResult result;
  PollOutcome &outcome = result.outcome;

  minijson::Value command;
  if (!minijson::Parse(payload_json.c_str(), &command) || command.type != minijson::Type::Object) {
    result.ack_json = "{\"id\": 0, \"ok\": false, \"error\": \"unparseable command\", "
                      "\"backend\": \"bridge\"}";
    return result;
  }

  int id = command.Int("id", 0);
  const minijson::Value *op_value = command.Get("op");
  if (op_value == nullptr || op_value->type != minijson::Type::String) {
    result.ack_json = "{\"id\": " + std::to_string(id) +
                      ", \"ok\": false, \"error\": \"command has no op\", \"backend\": \"bridge\"}";
    return result;
  }
  const std::string op = op_value->text;

  // Liveness probe: an ID-LESS ping is stateless - answered end to end
  // (transport -> queue -> tick -> ack) without touching the ledger, the
  // filesystem, or FL, so heartbeats can never evict real ids from the ledger
  // ring. The ack carries our proto so a mismatched client learns it from the
  // pong itself. A ping that CARRIES an id is a queued command like any other
  // and takes the claim path below - the claim is what prunes it from the v2
  // queue when the server dies before doing so (bridge-v2-sync seam).
  if (op == "ping" && command.Get("id") == nullptr) {
    outcome.had_command = true;
    outcome.op = op;
    outcome.ok = true;
    result.ack_json = "{\"id\": 0, \"ok\": true, \"op\": \"ping\", \"proto\": " +
                      std::to_string(kProtoVersion) + ", \"backend\": \"bridge\"}";
    return result;
  }

  if (AlreadyConsumed(roots, id)) {
    // A retransmit (or the other carrier / the pyscript got there first).
    // Re-ack the recorded result verbatim when we still have it; otherwise a
    // minimal consumed ack. Never re-execute.
    result.duplicate = true;
    outcome.id = id;
    outcome.op = op;
    const std::string *cached = cache != nullptr ? cache->Find(id) : nullptr;
    result.ack_json = cached != nullptr
                          ? *cached
                          : "{\"id\": " + std::to_string(id) +
                                ", \"ok\": true, \"consumed\": true, \"duplicate\": true, "
                                "\"backend\": \"bridge\"}";
    return result;
  }

  outcome.had_command = true;
  outcome.id = id;
  outcome.op = op;

  int proto = command.Int("proto", 0);
  if (proto != kProtoVersion) {
    RecordConsumed(roots, id);
    outcome.error = "protocol mismatch: command is v" + std::to_string(proto) + ", bridge is v" +
                    std::to_string(kProtoVersion);
    result.ack_json = "{\"id\": " + std::to_string(id) + ", \"ok\": false, \"error\": \"" +
                      minijson::Escape(outcome.error) + "\", \"backend\": \"bridge\"}";
    if (cache != nullptr) cache->Put(id, result.ack_json);
    WriteResult(roots, result.ack_json);
    return result;
  }

  // Claim before executing: a command that crashes FL must not replay on
  // restart and crash it again.
  int accepted = RecordConsumed(roots, id);

  if (op == "ping") {
    // Claimed like any id-carrying command (queue hygiene); no host calls.
    outcome.ok = true;
  } else if (op == "add_notes" || op == "clear_notes") {
    minijson::Value clear_cmd;
    const minijson::Value *apply_cmd = &command;
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
  json += ", \"spool\": \"" + minijson::Escape(carrier) + "\"";
  json += ", \"summary\": \"applied " + std::to_string(outcome.notes_written) +
          " note(s) via FHD_AddNotesToPR\"}";
  result.ack_json = json;
  if (cache != nullptr) cache->Put(id, json);
  WriteResult(roots, json);
  return result;
}

// --- the file-spool carrier ---------------------------------------------------

PollOutcome PollOnce(TFruityPlugHost *host, std::intptr_t host_tag,
                     const std::vector<std::string> &roots, int ppq_default,
                     ResultCache *cache) {
  // Find the newest readable pending command across roots.
  std::string text;
  std::string source_root;
  for (const std::string &root : roots) {
    std::string candidate;
    if (!ReadFile(Join(root, "pending.json"), &candidate)) continue;
    minijson::Value parsed;
    if (!minijson::Parse(candidate.c_str(), &parsed)) continue;
    if (parsed.type != minijson::Type::Object) continue;
    const minijson::Value *op = parsed.Get("op");
    if (op == nullptr || op->type != minijson::Type::String) continue;  // tombstone or malformed
    text = candidate;
    source_root = root;
    break;  // preferred root first; one command per poll
  }
  if (source_root.empty()) return PollOutcome();  // nothing to do

  ExecResult result = ExecuteCommand(host, host_tag, roots, ppq_default, text, source_root, cache);

  // Mailbox disposal: tombstone pending.json wherever it exists (commands are
  // mirrored to every root) once the command is settled - claimed, refused, or
  // a duplicate. The mailbox must clear or this file would repoll forever.
  if (result.outcome.had_command || result.duplicate) {
    std::string tombstone =
        "{\"consumed\": true, \"id\": " + std::to_string(result.outcome.id) + "}";
    for (const std::string &root : roots) {
      std::string pending = Join(root, "pending.json");
      std::string existing;
      if (ReadFile(pending, &existing)) WriteFile(pending, tombstone);
    }
  }
  return result.outcome;
}

}  // namespace bridge_core
