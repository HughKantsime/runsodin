import { StrictMode } from 'react'
import { beforeEach, afterEach, expect, test, vi } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import useWebRTC from './useWebRTC'
let peers:any[],offers:any[],locals:any[],remotes:any[],fetchMock:any
const deferred=()=>{let resolve:any,reject:any;const promise=new Promise((a,b)=>{resolve=a;reject=b});return {promise,resolve,reject}}
function Harness({id=9,enabled=true,maxRetries=5}:any){const {videoRef,status,retry,fallback}=useWebRTC(id,{enabled,maxRetries});return <><video ref={videoRef} data-testid="video"/><output>{status}</output><span data-testid="fallback">{String(fallback)}</span><button onClick={retry}>Retry</button></>}
const flush=async()=>{await act(async()=>{await Promise.resolve();await Promise.resolve()})}
beforeEach(()=>{
 vi.useFakeTimers();peers=[];offers=[];locals=[];remotes=[]
 vi.stubGlobal('RTCPeerConnection',vi.fn(function(){const peer={close:vi.fn(),addTransceiver:vi.fn(),createOffer:vi.fn(()=>offers.shift()??Promise.resolve({type:'offer',sdp:'synthetic-offer'})),setLocalDescription:vi.fn(()=>locals.shift()??Promise.resolve()),setRemoteDescription:vi.fn(()=>remotes.shift()??Promise.resolve()),iceConnectionState:'new',ontrack:null,oniceconnectionstatechange:null};peers.push(peer);return peer}))
 vi.stubGlobal('RTCSessionDescription',function(value:any){return value})
 fetchMock=vi.fn(async()=>({ok:true,text:async()=>'synthetic-answer'}));vi.stubGlobal('fetch',fetchMock)
})
afterEach(()=>{vi.useRealTimers();vi.unstubAllGlobals()})
test('normal connection exchanges SDP with credentialed camera route',async()=>{
 render(<Harness/>);await flush()
 expect(fetchMock).toHaveBeenCalledWith('/api/cameras/9/webrtc',expect.objectContaining({method:'POST',credentials:'include',body:'synthetic-offer',headers:{'Content-Type':'application/sdp'}}))
 expect(peers[0].addTransceiver).toHaveBeenCalledWith('video',{direction:'recvonly'})
 expect(peers[0].setRemoteDescription).toHaveBeenCalledWith({type:'answer',sdp:'synthetic-answer'})
 const stream={id:'synthetic-stream'};act(()=>peers[0].ontrack({streams:[stream]}))
 fireEvent.loadedData(screen.getByTestId('video'));expect(screen.getByTestId('video')).toHaveProperty('srcObject',stream);expect(screen.getByText('live')).toBeInTheDocument()
})
test('unmount during pending offer never begins signaling or retry',async()=>{
 const pending=deferred();offers.push(pending.promise);const {unmount}=render(<Harness/>);unmount()
 pending.resolve({type:'offer',sdp:'synthetic-offer'});await flush()
 expect(fetchMock).not.toHaveBeenCalled();expect(peers[0].close).toHaveBeenCalled();expect(vi.getTimerCount()).toBe(0)
})
test('unmount while signaling fails cannot schedule a new connection',async()=>{
 const pending=deferred();fetchMock.mockReturnValueOnce(pending.promise);const {unmount}=render(<Harness/>);await flush();unmount()
 pending.reject(new Error('Synthetic signaling failure'));await flush();expect(vi.getTimerCount()).toBe(0)
 await act(async()=>vi.advanceTimersByTime(4000));expect(peers).toHaveLength(1)
})
test('camera switch ignores obsolete signaling answer and peer callbacks',async()=>{
 const pending=deferred();fetchMock.mockReturnValueOnce(pending.promise);const {rerender}=render(<Harness id={9}/>);await flush();const old=peers[0],oldTrack=old.ontrack
 rerender(<Harness id={10}/>);await flush();pending.resolve({ok:true,text:async()=>'obsolete-answer'});await flush()
 expect(old.setRemoteDescription).not.toHaveBeenCalled();act(()=>oldTrack({streams:[{id:'obsolete-stream'}]}));expect(screen.getByText('connecting')).toBeInTheDocument()
 expect(screen.getByTestId('video')).not.toHaveProperty('srcObject',expect.objectContaining({id:'obsolete-stream'}))
})
test('manual retry cannot start a disabled camera',async()=>{render(<Harness enabled={false}/>);fireEvent.click(screen.getByRole('button',{name:'Retry'}));await flush();expect(peers).toHaveLength(0);expect(fetchMock).not.toHaveBeenCalled()})
test('disabling a live camera releases displayed media and pending work',async()=>{
 const {rerender}=render(<Harness/>);await flush();act(()=>peers[0].ontrack({streams:[{id:'synthetic-stream'}]}));rerender(<Harness enabled={false}/>);await flush()
 expect(screen.getByTestId('video')).toHaveProperty('srcObject',null);expect(peers[0].close).toHaveBeenCalled();expect(vi.getTimerCount()).toBe(0)
})
test('disabling during signaling aborts request and ignores late rejection',async()=>{
 const pending=deferred();fetchMock.mockReturnValueOnce(pending.promise);const {rerender}=render(<Harness/>);await flush()
 const signal=fetchMock.mock.calls[0][1].signal;rerender(<Harness enabled={false}/>);expect(signal.aborted).toBe(true)
 pending.reject(new Error('Synthetic disabled response'));await flush();expect(vi.getTimerCount()).toBe(0);expect(peers).toHaveLength(1)
})
test('obsolete answer body cannot configure a switched peer',async()=>{
 const body=deferred();fetchMock.mockResolvedValueOnce({ok:true,text:()=>body.promise});const {rerender}=render(<Harness/>);await flush()
 rerender(<Harness id={10}/>);await flush();body.resolve('obsolete-body');await flush()
 expect(peers[0].setRemoteDescription).not.toHaveBeenCalled();expect(peers[1].setRemoteDescription).toHaveBeenCalledWith({type:'answer',sdp:'synthetic-answer'})
})
test('obsolete ICE failure cannot close or retry the current camera',async()=>{
 const {rerender}=render(<Harness/>);await flush();const old=peers[0],callback=old.oniceconnectionstatechange
 rerender(<Harness id={10}/>);await flush();old.iceConnectionState='failed';act(()=>callback())
 expect(peers[1].close).not.toHaveBeenCalled();expect(vi.getTimerCount()).toBe(1);expect(screen.getByText('connecting')).toBeInTheDocument()
})
test('signaling failure closes peers and respects exponential retry cap',async()=>{
 fetchMock.mockResolvedValue({ok:false,status:503});render(<Harness maxRetries={2}/>);await flush()
 expect(screen.getByText('error')).toBeInTheDocument();expect(peers[0].close).toHaveBeenCalled()
 await act(async()=>vi.advanceTimersByTime(1999));expect(peers).toHaveLength(1)
 await act(async()=>vi.advanceTimersByTime(1));await flush();expect(peers).toHaveLength(2)
 await act(async()=>vi.advanceTimersByTime(3999));expect(peers).toHaveLength(2)
 await act(async()=>vi.advanceTimersByTime(1));await flush();expect(peers).toHaveLength(3);expect(vi.getTimerCount()).toBe(1)
 fireEvent.click(screen.getByRole('button',{name:'Retry'}));await flush();expect(peers).toHaveLength(4);expect(vi.getTimerCount()).toBe(2)
})
test('transient ICE disconnection recovers in grace period without another peer',async()=>{
 render(<Harness/>);await flush();const peer=peers[0]
 act(()=>peer.ontrack({streams:[{id:'synthetic-stream'}]}));peer.iceConnectionState='disconnected';act(()=>peer.oniceconnectionstatechange())
 expect(screen.getByText('disconnected')).toBeInTheDocument();await act(async()=>vi.advanceTimersByTime(4000))
 Object.defineProperty(screen.getByTestId('video'),'readyState',{value:2,configurable:true});peer.iceConnectionState='connected';act(()=>peer.oniceconnectionstatechange());expect(screen.getByText('live')).toBeInTheDocument()
 await act(async()=>vi.advanceTimersByTime(5000));expect(peers).toHaveLength(1);expect(vi.getTimerCount()).toBe(0)
})
test('persistent ICE disconnection closes old peer and retries after grace',async()=>{
 render(<Harness/>);await flush();const peer=peers[0];peer.iceConnectionState='disconnected';act(()=>peer.oniceconnectionstatechange())
 await act(async()=>vi.advanceTimersByTime(5000));expect(peer.close).toHaveBeenCalled();expect(peers).toHaveLength(1)
 await act(async()=>vi.advanceTimersByTime(2000));await flush();expect(peers).toHaveLength(2)
})
test('manual retry invalidates pending offer rather than opening overlapping peers',async()=>{
 const pending=deferred();offers.push(pending.promise);render(<Harness/>);fireEvent.click(screen.getByRole('button',{name:'Retry'}));await flush()
 pending.resolve({type:'offer',sdp:'obsolete-offer'});await flush()
 expect(peers).toHaveLength(2);expect(peers[0].close).toHaveBeenCalled();expect(fetchMock).toHaveBeenCalledTimes(1)
 expect(fetchMock.mock.calls[0][1].body).toBe('synthetic-offer')
})
test('missing camera identity does not connect even on retry',async()=>{render(<Harness id={null}/>);fireEvent.click(screen.getByRole('button',{name:'Retry'}));await flush();expect(peers).toHaveLength(0)})
test('unmount during pending local description prevents signaling',async()=>{
 const pending=deferred();locals.push(pending.promise);const {unmount}=render(<Harness/>);await flush();unmount()
 pending.resolve();await flush();expect(fetchMock).not.toHaveBeenCalled();expect(peers[0].close).toHaveBeenCalled();expect(vi.getTimerCount()).toBe(0)
})
test('late remote description failure after unmount cannot retry',async()=>{
 const pending=deferred();remotes.push(pending.promise);const {unmount}=render(<Harness/>);await flush();unmount()
 pending.reject(new Error('Synthetic remote description failure'));await flush();expect(peers[0].close).toHaveBeenCalled();expect(vi.getTimerCount()).toBe(0)
})
test('StrictMode obsolete setup cannot signal after cleanup',async()=>{
 const pending=deferred();offers.push(pending.promise);render(<StrictMode><Harness/></StrictMode>);await flush()
 pending.resolve({type:'offer',sdp:'obsolete-offer'});await flush();expect(peers).toHaveLength(2);expect(peers[0].close).toHaveBeenCalled();expect(fetchMock).toHaveBeenCalledTimes(1);expect(peers[1].close).not.toHaveBeenCalled()
})
test('peer constructor failure is visible and retries within the cap',async()=>{
 vi.mocked(RTCPeerConnection).mockImplementationOnce(function(){throw new Error('Synthetic unavailable peer')})
 render(<Harness maxRetries={1}/>);await flush();expect(screen.getByText('error')).toBeInTheDocument();expect(peers).toHaveLength(0)
 await act(async()=>vi.advanceTimersByTime(2000));await flush();expect(peers).toHaveLength(1);expect(vi.getTimerCount()).toBe(1)
})

test('SDP track without decoded video falls back after twelve seconds',async()=>{
 const {unmount}=render(<Harness/>);await flush()
 act(()=>peers[0].ontrack({streams:[{id:'no-frames'}]}))
 expect(screen.getByText('connecting')).toBeInTheDocument()
 await act(async()=>vi.advanceTimersByTime(12000))
 expect(screen.getByTestId('fallback')).toHaveTextContent('true')
 fireEvent.loadedData(screen.getByTestId('video'))
 expect(screen.getByText('live')).toBeInTheDocument()
 expect(screen.getByTestId('fallback')).toHaveTextContent('false')
 unmount();expect(vi.getTimerCount()).toBe(0)
})
