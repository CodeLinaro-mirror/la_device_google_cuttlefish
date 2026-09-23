/*
 * Copyright (C) 2026 The Android Open Source Project
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *      http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

package com.android.cuttlefish.audiotest

import android.content.Context
import android.media.AudioAttributes
import android.media.AudioFocusRequest
import android.media.AudioManager
import android.os.Handler
import android.os.Looper
import android.util.Log
import androidx.annotation.GuardedBy
import androidx.test.platform.app.InstrumentationRegistry
import com.google.android.mobly.snippet.Snippet
import com.google.android.mobly.snippet.event.EventCache
import com.google.android.mobly.snippet.event.SnippetEvent
import com.google.android.mobly.snippet.rpc.AsyncRpc
import com.google.android.mobly.snippet.rpc.Rpc

/**
 * Mobly Snippet exposing Android AudioFocus operations over JSON-RPC.
 *
 * Keyed by [clientId] so multiple independent focus clients can be created,
 * observed, and abandoned concurrently within the same test session.
 */
class AudioFocusSnippet : Snippet {
    private val instrumentation = InstrumentationRegistry.getInstrumentation()
    private val context: Context = instrumentation.targetContext
    private val audioManager = context.getSystemService(Context.AUDIO_SERVICE) as AudioManager
    private val mainHandler = Handler(Looper.getMainLooper())
    private val lock = Any()

    @GuardedBy("lock")
    private val activeRequests = mutableMapOf<String, MutableList<AudioFocusRequest>>()

    @GuardedBy("lock")
    private val callbackIds = mutableMapOf<String, String>()

    init {
        instrumentation.uiAutomation.adoptShellPermissionIdentity()
    }

    /**
     * Requests audio focus for [clientId] with the specified [usage], [contentType], and
     * [focusGain].
     */
    @Rpc(description = "Requests audio focus for clientId with given attributes and gain type")
    fun requestAudioFocus(
        clientId: String,
        usage: Int,
        contentType: Int,
        focusGain: Int,
    ): Int {
        val attributes =
            AudioAttributes.Builder()
                .setUsage(usage)
                .setContentType(contentType)
                .build()

        val listener =
            AudioManager.OnAudioFocusChangeListener { focusChange ->
                Log.d(TAG, "onAudioFocusChange: clientId=$clientId focusChange=$focusChange")
                val callbackId = synchronized(lock) { callbackIds[clientId] }
                if (callbackId != null) {
                    val event =
                        SnippetEvent(callbackId, EVENT_ON_AUDIO_FOCUS_CHANGE).apply {
                            data.putString(FIELD_CLIENT_ID, clientId)
                            data.putInt(FIELD_FOCUS_CHANGE, focusChange)
                        }
                    EventCache.getInstance().postEvent(event)
                }
            }

        val request =
            AudioFocusRequest.Builder(focusGain)
                .setAudioAttributes(attributes)
                .setAcceptsDelayedFocusGain(true)
                .setOnAudioFocusChangeListener(listener, mainHandler)
                .build()

        val result = audioManager.requestAudioFocus(request)
        Log.d(
            TAG,
            "requestAudioFocus: clientId=$clientId usage=$usage contentType=$contentType " +
                "focusGain=$focusGain result=$result",
        )
        if (result == AudioManager.AUDIOFOCUS_REQUEST_GRANTED ||
            result == AudioManager.AUDIOFOCUS_REQUEST_DELAYED
        ) {
            synchronized(lock) {
                activeRequests.getOrPut(clientId) { mutableListOf() }.add(request)
            }
        }
        return result
    }

    /** Abandons all active [AudioFocusRequest]s associated with [clientId]. */
    @Rpc(description = "Abandons audio focus for clientId")
    fun abandonAudioFocus(clientId: String): Int {
        val requests =
            synchronized(lock) {
                callbackIds.remove(clientId)
                activeRequests.remove(clientId)
            }
        if (requests.isNullOrEmpty()) {
            Log.w(TAG, "abandonAudioFocus: no active request for clientId=$clientId")
            return AudioManager.AUDIOFOCUS_REQUEST_FAILED
        }
        var lastResult = AudioManager.AUDIOFOCUS_REQUEST_GRANTED
        for (request in requests) {
            lastResult = audioManager.abandonAudioFocusRequest(request)
            Log.d(TAG, "abandonAudioFocus: clientId=$clientId result=$lastResult")
        }
        return lastResult
    }

    /** Abandons all active audio focus requests to ensure test isolation. */
    @Rpc(description = "Abandons all active audio focus requests")
    fun abandonAllAudioFocus() {
        val requestsByClient =
            synchronized(lock) {
                val copy = activeRequests.mapValues { it.value.toList() }
                activeRequests.clear()
                callbackIds.clear()
                copy
            }
        for ((clientId, requests) in requestsByClient) {
            for (request in requests) {
                val result = audioManager.abandonAudioFocusRequest(request)
                Log.d(TAG, "abandonAllAudioFocus: clientId=$clientId result=$result")
            }
        }
    }

    /** Registers an asynchronous callback for focus change events on [clientId]. */
    @AsyncRpc(description = "Registers an audio focus change listener for clientId")
    fun registerFocusChangeListener(callbackId: String, clientId: String) {
        Log.d(TAG, "registerFocusChangeListener: clientId=$clientId callbackId=$callbackId")
        synchronized(lock) {
            callbackIds[clientId] = callbackId
        }
    }

    /** Unregisters any asynchronous focus change listener associated with [clientId]. */
    @Rpc(description = "Unregisters an audio focus change listener for clientId")
    fun unregisterFocusChangeListener(clientId: String) {
        Log.d(TAG, "unregisterFocusChangeListener: clientId=$clientId")
        synchronized(lock) {
            callbackIds.remove(clientId)
        }
    }

    override fun shutdown() {
        abandonAllAudioFocus()
        instrumentation.uiAutomation.dropShellPermissionIdentity()
    }

    private companion object {
        const val TAG = "AudioFocusSnippet"
        const val EVENT_ON_AUDIO_FOCUS_CHANGE = "onAudioFocusChange"
        const val FIELD_CLIENT_ID = "clientId"
        const val FIELD_FOCUS_CHANGE = "focusChange"
    }
}
