"""기존 KHOpenAPI OCX 로그인 유지. TR 데이터는 수신 콜백 안에서 복사."""
import os
import sys
import time
from collections import deque
from PyQt5.QAxContainer import QAxWidget
from PyQt5.QtCore import QEventLoop, QTimer
from version_guard import VERSION_EXIT,KOA,VersionWatch,claim_single_instance,running,wait_updater

FUT_FIELDS={10:'fut_price',195:'fut_oi',181:'fut_oi_prev',183:'basis',184:'basis_theory',197:'k200'}
OPT_FIELDS={10:'price',195:'oi',189:'iv',190:'delta'}

class OpenApiBusy(RuntimeError):pass

class Kiwoom:
    def __init__(self,log=print,module_dir='D:/OpenAPI'):
        self.log=log;self.module_dir=module_dir
        # 두 프로세스가 OCX를 잡으면 버전처리 파일 교체가 실패하므로 이 폴더의 실행기는 하나만 허용.
        if not claim_single_instance():
            raise OpenApiBusy('다른 수집기 또는 투자자수급점검 실행창이 이미 Open API를 사용 중입니다. 그 창을 닫고 다시 실행하세요.')
        self.ocx=QAxWidget('KHOPENAPI.KHOpenAPICtrl.1')
        if self.ocx.isNull():raise RuntimeError('32비트 Open API+ OCX를 불러오지 못했습니다')
        self.ocx.OnEventConnect.connect(self._connect)
        self.ocx.OnReceiveTrData.connect(self._tr)
        self.ocx.OnReceiveRealData.connect(self._real)
        self.ocx.OnReceiveMsg.connect(self._msg)
        self.login_loop=None;self.login_ok=False
        self.active=None;self.queue=deque();self.counter=0;self.last_request=0
        self.on_real=None;self.on_message=None
        self.pump=QTimer();self.pump.timeout.connect(self._pump);self.pump.start(200)
        self.timeout=QTimer();self.timeout.setSingleShot(True);self.timeout.timeout.connect(self._timeout)

    def call(self,sig,*args):return self.ocx.dynamicCall(sig,*args)

    def login(self):
        if running(KOA):self.log('KOA Studio가 실행 중입니다. 버전처리가 나오면 KOA Studio도 닫아야 업데이트가 적용됩니다.')
        if not wait_updater(self.log):return False
        self.login_loop=QEventLoop()
        timer=QTimer();timer.setSingleShot(True);timer.timeout.connect(self.login_loop.quit);timer.start(600000)
        watch=VersionWatch(self.module_dir,self._version_exit).start()
        try:
            ret=self.call('CommConnect()')
            if ret!=0:return False
            if not self.login_ok:self.login_loop.exec_()
        finally:
            watch.stop();timer.stop();self.login_loop=None
        return self.login_ok

    def _version_exit(self,reason):
        # 팝업은 opstarter.exe 소유라 이 프로세스가 끝나도 남는다. 종료로 OCX·DLL 잠금을 푼 뒤 사용자가 [확인].
        try:self.log(reason+' · 파일 잠금을 풀기 위해 이 실행기를 종료합니다. 키움 팝업의 [확인]을 누르세요.')
        finally:
            sys.stdout.flush();os._exit(VERSION_EXIT)

    def _connect(self,error):
        self.login_ok=error==0
        self.log('로그인 성공' if self.login_ok else '로그인 실패 코드 '+str(error))
        if self.login_loop:self.login_loop.quit()

    def request(self,spec,inputs,done,prev_next=0):
        self.counter+=1
        self.queue.append({'spec':spec,'inputs':inputs,'done':done,'rq':'radar_%d'%self.counter,'prev_next':prev_next})

    def _pump(self):
        if self.active or not self.queue or time.monotonic()-self.last_request<.7:return
        if self.call('GetConnectState()')!=1:return
        self.active=self.queue.popleft();a=self.active
        for name,value in a['inputs'].items():self.call('SetInputValue(QString, QString)',name,str(value))
        self.last_request=time.monotonic()
        ret=self.call('CommRqData(QString, QString, int, QString)',a['rq'],a['spec']['trcode'],a.get('prev_next',0),'7101')
        if ret!=0:
            self.active=None;a['done'](None,'요청 오류 코드 '+str(ret));return
        self.timeout.start(8000)

    def _tr(self,screen,rq,tr,record,prev,*rest):
        a=self.active
        if not a or screen!='7101' or rq!=a['rq'] or tr.lower()!=a['spec']['trcode'].lower():return
        result={}
        # 수신 버퍼가 유효한 콜백 안에서 반복부와 모든 필드를 복사.
        for rec,fields in a['spec']['records'].items():
            cnt=int(self.call('GetRepeatCnt(QString, QString)',tr,rec) or 0)
            count=min(max(cnt,1),1500)
            result[rec]=[{f:str(self.call('GetCommData(QString, QString, int, QString)',tr,rec,i,f) or '').strip() for f in fields} for i in range(count)]
        self.timeout.stop();self.active=None
        a['done'](result, '연속 조회 잔여' if str(prev)=='2' else '')

    def _timeout(self):
        a=self.active;self.active=None
        if a:a['done'](None,'응답 시간 초과. 서버 지원 및 입력 값을 확인하세요')

    def _msg(self,screen,rq,tr,msg):
        if self.on_message:self.on_message(tr,str(msg))

    def _real(self,code,kind,data):
        if self.on_real:self.on_real(str(code),str(kind))

    def real(self,code,fid):return str(self.call('GetCommRealData(QString, int)',code,fid) or '').strip()
    def register(self,screen,codes,fids):
        ret=self.call('SetRealReg(QString, QString, QString, QString)',screen,';'.join(codes),';'.join(map(str,fids)),'0')
        if ret not in (0,None):self.log('실시간 등록 결과 '+str(ret))

    def remove(self,screen):self.call('SetRealRemove(QString, QString)',screen,'ALL')
