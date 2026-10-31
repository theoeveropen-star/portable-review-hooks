"""Bounded UTF-8 reads below a pinned directory, with no symlink traversal."""
import os, stat
class ReadError(Exception):pass
class Reader:
 def __init__(self,root):
  self.fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
 def close(self):os.close(self.fd)
 def open(self,path,directory=False):
  if not isinstance(path,str) or len(path.encode())>1024 or '\x00' in path or path.startswith('/') or '\\' in path:raise ReadError('invalid_path')
  parts=[] if path=='.' else path.split('/')
  if any(p in ('','.','..') for p in parts):raise ReadError('invalid_path')
  fd=os.dup(self.fd)
  try:
   for i,part in enumerate(parts):
    flags=os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC
    if i<len(parts)-1 or directory:flags|=os.O_DIRECTORY
    # Avoid blocking on a FIFO before its type is known.
    flags|=os.O_NONBLOCK
    nxt=os.open(part,flags,dir_fd=fd);os.close(fd);fd=nxt
   meta=os.fstat(fd)
   if directory and not stat.S_ISDIR(meta.st_mode) or not directory and not stat.S_ISREG(meta.st_mode):raise ReadError('not_regular')
   return fd
  except Exception:
   os.close(fd);raise
 def call(self,method,args):
  if not isinstance(args,dict) or method not in ('read','list','search'):raise ReadError('invalid_request')
  if set(args)!=({'path','query'} if method=='search' else {'path'}):raise ReadError('invalid_request')
  if method=='search' and (not isinstance(args['query'],str) or not args['query'] or len(args['query'].encode())>256 or '\n' in args['query']):raise ReadError('invalid_query')
  fd=self.open(args['path'],method=='list')
  try:
   if method=='list':
    names=[]
    with os.scandir(fd) as entries:
     for entry in entries:
      names.append(entry.name)
      if len(names)>100 or sum(len(n.encode()) for n in names)>4096:raise ReadError('directory_too_large')
    return '\n'.join(sorted(names))
   before=os.fstat(fd)
   if before.st_size>4096:raise ReadError('file_too_large')
   chunks=[];size=0
   while size<=4096:
    chunk=os.read(fd,4097-size)
    if not chunk:break
    chunks.append(chunk);size+=len(chunk)
   after=os.fstat(fd)
   if size>4096:raise ReadError('file_too_large')
   if (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns) or size!=before.st_size:raise ReadError('file_changed')
   text=b''.join(chunks).decode('utf-8')
   if method=='read':return text
   matches=[f'{n}:{line}' for n,line in enumerate(text.splitlines(),1) if args['query'] in line]
   result='\n'.join(matches)
   if len(result.encode())>4096:raise ReadError('matches_too_large')
   return result
  finally:os.close(fd)
