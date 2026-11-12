"""Atomic per-document checkpoints. No credentials; never a deliverable PDF."""
import hashlib
import json
from pathlib import Path
import shutil
import threading
from .config import atomic_json, sha256_file

STAGE_FILES = {
    'inspection': ['engine-work/inspection-ir/source/styles_and_formulas.json'],
    'clean': ['engine-work/clean/source.zh-CN.mono.pdf', 'engine-work/clean-ir/source/final-layout.json'],
}

class TranslationCheckpoint:
    def __init__(self, path, scope):
        self.path = Path(path)
        self.scope = scope
        self.lock = threading.RLock()
        self.entries = {}
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
            if data.get('schema') == 1 and data.get('scope') == scope:
                self.entries = {k:v for k,v in data.get('entries',{}).items()
                                if isinstance(k,str) and isinstance(v,str)}
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        self.hits = 0

    @staticmethod
    def key(context, row):
        # BabelDOC paragraph IDs may change between runs; keep content/context
        # and all other row fields, and rebind output IDs in the current run.
        payload = [context, {k:v for k,v in row.items() if k != 'id'}]
        return hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=False).encode()).hexdigest()

    def get(self, key):
        with self.lock:
            value = self.entries.get(key)
            if value is not None: self.hits += 1
            return value

    def save(self, values):
        if not values: return
        with self.lock:
            self.entries.update(values)
            atomic_json(self.path, {'schema':1, 'scope':self.scope, 'entries':self.entries})

class SharedMemory(TranslationCheckpoint):
    def save(self,values):
        if not values:return
        from filelock import FileLock
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.lock,FileLock(str(self.path)+'.lock',timeout=15):
            fresh=TranslationCheckpoint(self.path,self.scope)
            self.entries=fresh.entries
            super().save(values)

class DurableCheckpoint(TranslationCheckpoint):
    """Completed paragraph translations survive layout upgrades and job cleanup."""
    def __init__(self,path,scope,memory_path,memory_scope):
        super().__init__(path,scope)
        self.memory=SharedMemory(memory_path,memory_scope)
        self.entries={**self.memory.entries,**self.entries}

    def save(self,values):
        super().save(values)
        self.memory.save(values)

def scope_of(state):
    return {key:state[key] for key in ('sourceHash','configFingerprint')}

def stage_ready(job, state, stage):
    try:
        data = json.loads((job/'stages.json').read_text(encoding='utf-8'))
        files = data['stages'][stage]
        return (data['scope'] == scope_of(state) and set(files) == set(STAGE_FILES[stage])
                and all((job/name).is_file() and sha256_file(job/name) == digest for name,digest in files.items()))
    except (OSError, ValueError, KeyError, TypeError): return False

def save_stage(job, state, stage):
    data = {'scope':scope_of(state), 'stages':{}}
    for key in STAGE_FILES:
        if key == stage or stage_ready(job,state,key):
            data['stages'][key] = {name:sha256_file(job/name) for name in STAGE_FILES[key]}
    atomic_json(job/'stages.json',data)

def copy_resume(previous, target, state, include_stages=True):
    checkpoint = TranslationCheckpoint(previous/'translation-checkpoint.json',scope_of(state))
    if checkpoint.entries:
        TranslationCheckpoint(target/'translation-checkpoint.json',scope_of(state)).save(checkpoint.entries)
    stages = []
    for stage in STAGE_FILES if include_stages else ():
        if stage_ready(previous,state,stage):
            for name in STAGE_FILES[stage]:
                (target/name).parent.mkdir(parents=True,exist_ok=True)
                shutil.copy2(previous/name,target/name)
            save_stage(target,state,stage); stages.append(stage)
    # Inspection diagnostics must follow the cached stage used for postflight.
    if 'inspection' in stages and (previous/'translation-records.json').is_file():
        shutil.copy2(previous/'translation-records.json',target/'translation-records.json')
    return {'savedTranslations':len(checkpoint.entries),'stages':stages}
