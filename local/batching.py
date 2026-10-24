"""Bounded source snapshots for dynamic JSONL batches, without editing adapters."""
import os


class LimitedReader:
    def __init__(self,stream,limit):self.stream=stream;self.limit=limit
    def __enter__(self):return self
    def __exit__(self,*args):self.stream.close()
    def __iter__(self):return self
    def __next__(self):
        line=self.readline()
        if not line:raise StopIteration
        return line
    def read(self,size=-1):
        left=max(0,self.limit-self.stream.tell())
        return self.stream.read(left if size<0 else min(size,left))
    def readline(self,size=-1):
        left=max(0,self.limit-self.stream.tell())
        return self.stream.readline(left if size<0 else min(size,left))
    def seek(self,offset,whence=os.SEEK_SET):
        if whence==os.SEEK_END:offset+=self.limit;whence=os.SEEK_SET
        return self.stream.seek(offset,whence)
    def tell(self):return self.stream.tell()


class SnapshotStat:
    def __init__(self,value,limit):self.value=value;self.st_size=min(limit,value.st_size)
    def __getattr__(self,name):return getattr(self.value,name)


class PrefixPath:
    def __init__(self,path,limit):self.path=path;self.limit=limit
    def __str__(self):return str(self.path)
    def __fspath__(self):return str(self.path)
    def __getattr__(self,name):return getattr(self.path,name)
    def stat(self):return SnapshotStat(self.path.stat(),self.limit)
    def open(self,mode='r',*args,**kwargs):
        if mode!='rb':raise ValueError('Bounded source snapshots are read-only binary streams')
        return LimitedReader(self.path.open(mode,*args,**kwargs),self.limit)
    def read_bytes(self):
        with self.open('rb') as stream:return stream.read()


class Boundaries:
    """Collect only current-file record end offsets, not a cross-file spool."""
    def __init__(self,path,start):
        self.path=path;self.start=start;self.ends=[];self.eof=False
    def extend(self,source_bytes):
        start=self.ends[-1] if self.ends else self.start;total=0
        with self.path.open('rb') as stream:
            stream.seek(start)
            while total<source_bytes:
                line=stream.readline()
                if not line:self.eof=True;break
                total+=len(line);self.ends.append(stream.tell())
        return self.ends[-1] if self.ends else self.start
