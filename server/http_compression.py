"""Small gzip response hook; cloud runtime needs no native compression packages."""
import gzip
from flask import request


def compress_response(response):
    if (request.method=='HEAD' or response.status_code!=200 or response.direct_passthrough
            or response.headers.get('Content-Encoding')
            or response.mimetype not in ('application/json','text/html','text/plain','text/css','application/javascript')):
        return response
    response.vary.add('Accept-Encoding')
    if request.accept_encodings['gzip']<=0:return response
    body=response.get_data()
    if len(body)<1024:return response
    compressed=gzip.compress(body,compresslevel=1,mtime=0)
    if len(compressed)<len(body):
        response.set_data(compressed);response.headers['Content-Encoding']='gzip'
    return response
