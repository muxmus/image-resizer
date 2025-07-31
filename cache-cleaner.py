import os
import time
import datetime

print('cache-cleaner\n')

dirToBeEmptied = 'static/'

while True:

	ds = list(os.walk(dirToBeEmptied))
	delta = datetime.timedelta(days=30)
	now = datetime.datetime.now()

	n = 0

	for d in ds:
		os.chdir(d[0])
		if d[2] != []:
			for x in d[2]:
				atime = datetime.datetime.fromtimestamp(os.path.getatime(x))
				if atime < (now-delta):
					os.remove(x)
					n += 1

	print(datetime.datetime.now().strftime('%Y.%m.%d %H:%M:%S'), end=' - ')
	if n == 0:
		print('没有已过期的缓存')
	else:
		print(f'共删除了{n}张缓存图片')

	dirToBeEmptied = './'

	time.sleep(86400)
