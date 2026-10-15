import { useState, useEffect, useCallback } from 'react';

const API_BASE = '/api';

/**
 * Hook for managing push notification subscriptions
 */
export function usePushNotifications() {
  const [isSupported, setIsSupported] = useState(false);
  const [isSubscribed, setIsSubscribed] = useState(false);
  const [permission, setPermission] = useState('default');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  // Check if push is supported
  useEffect(() => {
    const supported = 'serviceWorker' in navigator && 'PushManager' in window;
    setIsSupported(supported);
    
    if (supported) {
      setPermission(Notification.permission);
      checkSubscription();
    } else {
      setLoading(false);
    }
  }, []);

  // Check current subscription status. A subscription bound to an older VAPID
  // key (the server rotated its pair) is replaced transparently while
  // notification permission is still granted.
  const checkSubscription = async () => {
    try {
      const registration = await navigator.serviceWorker.ready;
      const subscription = await registration.pushManager.getSubscription();
      if (subscription) {
        const serverKey = await fetchServerKey();
        const currentKey = subscriptionKey(subscription);
        if (serverKey && currentKey && currentKey !== serverKey) {
          await subscription.unsubscribe();
          if (Notification.permission === 'granted') {
            await subscribeWithKey(registration, serverKey);
            setIsSubscribed(true);
          } else {
            setIsSubscribed(false);
          }
          return;
        }
      }
      setIsSubscribed(!!subscription);
    } catch (err) {
      console.error('Error checking subscription:', err);
    } finally {
      setLoading(false);
    }
  };

  // Subscribe to push notifications
  const subscribe = useCallback(async () => {
    if (!isSupported) {
      setError('Push notifications not supported');
      return false;
    }

    setLoading(true);
    setError(null);

    try {
      // Request permission
      const perm = await Notification.requestPermission();
      setPermission(perm);
      
      if (perm !== 'granted') {
        setError('Notification permission denied');
        setLoading(false);
        return false;
      }

      // Register service worker if not already
      let registration = await navigator.serviceWorker.getRegistration();
      if (!registration) {
        registration = await navigator.serviceWorker.register('/sw.js');
        await navigator.serviceWorker.ready;
      }

      // Get VAPID public key from server
      const publicKey = await fetchServerKey();
      if (!publicKey) {
        setError('Push notifications not configured on server');
        setLoading(false);
        return false;
      }

      await subscribeWithKey(registration, publicKey);

      setIsSubscribed(true);
      setLoading(false);
      return true;

    } catch (err) {
      console.error('Subscribe error:', err);
      setError(err.message);
      setLoading(false);
      return false;
    }
  }, [isSupported]);

  // Unsubscribe from push notifications
  const unsubscribe = useCallback(async () => {
    setLoading(true);
    setError(null);

    try {
      const registration = await navigator.serviceWorker.ready;
      const subscription = await registration.pushManager.getSubscription();
      
      if (subscription) {
        // Unsubscribe from browser
        await subscription.unsubscribe();
        
        // Remove from server
        ;
        const headers = { 'Content-Type': 'application/json' };
        ;

        await fetch(`${API_BASE}/push/subscribe`, {
          method: 'DELETE',
          headers,
          body: JSON.stringify({ endpoint: subscription.endpoint })
        });
      }

      setIsSubscribed(false);
      setLoading(false);
      return true;

    } catch (err) {
      console.error('Unsubscribe error:', err);
      setError(err.message);
      setLoading(false);
      return false;
    }
  }, []);

  return {
    isSupported,
    isSubscribed,
    permission,
    loading,
    error,
    subscribe,
    unsubscribe
  };
}

const KEY_CACHE = 'odin.push.vapidPublicKey';
const JSON_HEADERS = { 'Content-Type': 'application/json' };

async function fetchServerKey(): Promise<string | null> {
  const res = await fetch(`${API_BASE}/push/vapid-key`, { headers: JSON_HEADERS });
  const data = await res.json();
  return data?.enabled && data.public_key ? data.public_key : null;
}

function bytesToBase64Url(buffer: ArrayBuffer): string {
  let binary = '';
  new Uint8Array(buffer).forEach(byte => { binary += String.fromCharCode(byte); });
  return window.btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

// The applicationServerKey a subscription was created with: from the browser when
// it exposes it, else the key cached when this browser subscribed.
function subscriptionKey(subscription: any): string | null {
  const key = subscription?.options?.applicationServerKey;
  if (key && key.byteLength) return bytesToBase64Url(key);
  try { return window.localStorage.getItem(KEY_CACHE); } catch { return null; }
}

async function subscribeWithKey(registration: any, publicKey: string) {
  const subscription = await registration.pushManager.subscribe({
    userVisibleOnly: true,
    applicationServerKey: urlBase64ToUint8Array(publicKey),
  });
  // Send subscription to server in the PushSubscriptionCreate contract
  const json = subscription.toJSON();
  if (!json.endpoint || !json.keys?.p256dh || !json.keys?.auth) {
    throw new Error('Browser returned an incomplete push subscription');
  }
  const subRes = await fetch(`${API_BASE}/push/subscribe`, {
    method: 'POST',
    headers: JSON_HEADERS,
    body: JSON.stringify({ endpoint: json.endpoint, p256dh_key: json.keys.p256dh, auth_key: json.keys.auth })
  });
  if (!subRes.ok) {
    throw new Error('Failed to save subscription');
  }
  try { window.localStorage.setItem(KEY_CACHE, publicKey); } catch { /* storage unavailable */ }
}

// Helper: Convert URL-safe base64 to Uint8Array
function urlBase64ToUint8Array(base64String) {
  const padding = '='.repeat((4 - base64String.length % 4) % 4);
  const base64 = (base64String + padding)
    .replace(/-/g, '+')
    .replace(/_/g, '/');
  
  const rawData = window.atob(base64);
  const outputArray = new Uint8Array(rawData.length);
  
  for (let i = 0; i < rawData.length; ++i) {
    outputArray[i] = rawData.charCodeAt(i);
  }
  
  return outputArray;
}

export default usePushNotifications;
