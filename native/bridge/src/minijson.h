// A tiny JSON reader for the spool command schema, and nothing more.
//
// The Bridge deliberately has no third-party dependencies: it is a binary users
// drop into FL Studio's application bundle, so every dependency is one more
// thing that can fail to load inside someone else's DAW.
//
// The schema it must handle is flat and known (see docs/03):
//
//   {"proto":1,"id":7,"op":"add_notes","mode":"merge",
//    "notes":[{"key":60,"start":0.0,"length":1.0,"velocity":0.8}]}
//
// So this supports objects, arrays, numbers, strings, booleans and null - no
// nested arrays-of-arrays, no unicode escapes beyond passthrough. Anything it
// cannot represent is reported as a parse failure rather than silently ignored,
// because a half-read command would write the wrong notes.

#ifndef FLMCP_MINIJSON_H
#define FLMCP_MINIJSON_H

#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

namespace minijson {

class Value;
using Object = std::vector<std::pair<std::string, Value>>;
using Array = std::vector<Value>;

enum class Type { Null, Bool, Number, String, Object, Array };

class Value {
 public:
  Type type = Type::Null;
  bool boolean = false;
  double number = 0.0;
  std::string text;
  Object object;
  Array array;

  bool Has(const char* key) const {
    for (const auto& kv : object) {
      if (kv.first == key) return true;
    }
    return false;
  }

  const Value* Get(const char* key) const {
    for (const auto& kv : object) {
      if (kv.first == key) return &kv.second;
    }
    return nullptr;
  }

  double Num(const char* key, double fallback) const {
    const Value* v = Get(key);
    return (v != nullptr && v->type == Type::Number) ? v->number : fallback;
  }

  int Int(const char* key, int fallback) const {
    return static_cast<int>(Num(key, static_cast<double>(fallback)));
  }

  std::string Str(const char* key, const char* fallback) const {
    const Value* v = Get(key);
    return (v != nullptr && v->type == Type::String) ? v->text : std::string(fallback);
  }
};

class Parser {
 public:
  explicit Parser(const char* text) : p_(text) {}

  bool Parse(Value* out) {
    SkipSpace();
    if (!ParseValue(out)) return false;
    SkipSpace();
    return true;  // trailing bytes tolerated; the payload we care about is read
  }

 private:
  const char* p_;

  void SkipSpace() {
    while (*p_ != '\0' && (*p_ == ' ' || *p_ == '\t' || *p_ == '\n' || *p_ == '\r')) {
      ++p_;
    }
  }

  bool Literal(const char* word) {
    size_t n = strlen(word);
    if (strncmp(p_, word, n) != 0) return false;
    p_ += n;
    return true;
  }

  bool ParseString(std::string* out) {
    if (*p_ != '"') return false;
    ++p_;
    out->clear();
    while (*p_ != '\0' && *p_ != '"') {
      if (*p_ == '\\') {
        ++p_;
        switch (*p_) {
          case 'n': out->push_back('\n'); break;
          case 't': out->push_back('\t'); break;
          case 'r': out->push_back('\r'); break;
          case 'b': out->push_back('\b'); break;
          case 'f': out->push_back('\f'); break;
          case 'u': {
            // Not needed by our schema; keep the escape verbatim rather than
            // silently mangling text we do not understand.
            out->append("\\u");
            for (int i = 0; i < 4 && p_[1] != '\0'; ++i) out->push_back(*++p_);
            break;
          }
          default: out->push_back(*p_); break;
        }
        if (*p_ == '\0') return false;
        ++p_;
      } else {
        out->push_back(*p_++);
      }
    }
    if (*p_ != '"') return false;
    ++p_;
    return true;
  }

  bool ParseValue(Value* out) {
    SkipSpace();
    switch (*p_) {
      case '\0':
        return false;
      case '{': {
        ++p_;
        out->type = Type::Object;
        SkipSpace();
        if (*p_ == '}') { ++p_; return true; }
        while (true) {
          SkipSpace();
          std::string key;
          if (!ParseString(&key)) return false;
          SkipSpace();
          if (*p_ != ':') return false;
          ++p_;
          Value child;
          if (!ParseValue(&child)) return false;
          out->object.emplace_back(key, child);
          SkipSpace();
          if (*p_ == ',') { ++p_; continue; }
          if (*p_ == '}') { ++p_; return true; }
          return false;
        }
      }
      case '[': {
        ++p_;
        out->type = Type::Array;
        SkipSpace();
        if (*p_ == ']') { ++p_; return true; }
        while (true) {
          Value child;
          if (!ParseValue(&child)) return false;
          out->array.push_back(child);
          SkipSpace();
          if (*p_ == ',') { ++p_; continue; }
          if (*p_ == ']') { ++p_; return true; }
          return false;
        }
      }
      case '"':
        out->type = Type::String;
        return ParseString(&out->text);
      case 't':
        if (!Literal("true")) return false;
        out->type = Type::Bool;
        out->boolean = true;
        return true;
      case 'f':
        if (!Literal("false")) return false;
        out->type = Type::Bool;
        out->boolean = false;
        return true;
      case 'n':
        if (!Literal("null")) return false;
        out->type = Type::Null;
        return true;
      default: {
        char* end = nullptr;
        double value = strtod(p_, &end);
        if (end == p_) return false;
        p_ = end;
        out->type = Type::Number;
        out->number = value;
        return true;
      }
    }
  }
};

inline bool Parse(const char* text, Value* out) { return Parser(text).Parse(out); }

// Minimal escaping for the strings we emit (paths, error messages).
inline std::string Escape(const std::string& in) {
  std::string out;
  for (char c : in) {
    if (c == '"' || c == '\\') {
      out.push_back('\\');
      out.push_back(c);
    } else if (c == '\n') {
      out += "\\n";
    } else {
      out.push_back(c);
    }
  }
  return out;
}

}  // namespace minijson

#endif  // FLMCP_MINIJSON_H
