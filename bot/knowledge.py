import os
import re

try:
    import jieba
    jieba.setLogLevel(20)
    _HAS_JIEBA = True
except Exception:
    _HAS_JIEBA = False


class KnowledgeBase:
    def __init__(self, directory, top_k=3, max_chars=4000):
        self.directory = directory
        self.top_k = top_k
        self.max_chars = max_chars
        self.docs = []
        self.reload()

    def reload(self):
        self.docs = []
        if not os.path.isdir(self.directory):
            return
        for fn in sorted(os.listdir(self.directory)):
            if not fn.lower().endswith(".txt"):
                continue
            path = os.path.join(self.directory, fn)
            try:
                with open(path, encoding="utf-8") as fh:
                    text = fh.read()
            except Exception:
                continue
            if text.strip():
                self.docs.append({"name": fn, "text": text})
        return len(self.docs)

    def _terms(self, text):
        terms = set()
        for m in re.findall(r"[a-zA-Z0-9]+", text.lower()):
            if len(m) >= 2:
                terms.add(m)
        if _HAS_JIEBA:
            for w in jieba.lcut(text):
                w = w.strip().lower()
                if len(w) >= 2 and re.search(r"[\u4e00-\u9fff0-9a-z]", w):
                    terms.add(w)
        else:
            chinese = "".join(re.findall(r"[\u4e00-\u9fff]", text))
            for i in range(len(chinese) - 1):
                terms.add(chinese[i:i + 2])
            if chinese:
                terms.add(chinese)
        return terms

    def search(self, query, top_k=None, max_chars=None):
        top_k = top_k or self.top_k
        max_chars = max_chars or self.max_chars

        if not self.docs:
            return ""

        qterms = self._terms(query)
        if not qterms:
            return ""

        scored = []
        for doc in self.docs:
            dterms = self._terms(doc["text"])
            overlap = qterms & dterms
            if overlap:
                scored.append((len(overlap), doc))

        if not scored:
            return ""

        scored.sort(key=lambda x: x[0], reverse=True)

        out = []
        total = 0
        for score, doc in scored:
            if len(out) >= top_k:
                break
            chunk = "【资料：%s】\n%s\n" % (doc["name"], doc["text"])
            if total + len(chunk) > max_chars:
                chunk = chunk[: max(0, max_chars - total)]
            if not chunk:
                break
            out.append(chunk)
            total += len(chunk)
            if total >= max_chars:
                break

        return "\n".join(out)
