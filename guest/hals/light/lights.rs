/*
 * Copyright (C) 2023 The Android Open Source Project
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
//! This module implements the ILights AIDL interface.

use rustutils::android::system_properties;
use std::collections::HashMap;
use std::sync::Mutex;

use log::error;
use log::info;

use android_hardware_light::aidl::android::hardware::light::{
    HwLight::HwLight, HwLightEffect::HwLightEffect, HwLightState::HwLightState,
    HwPredefinedAnimation::HwPredefinedAnimation, ILights::ILights, LightType::LightType,
    PredefinedAnimationType::PredefinedAnimationType,
};

use binder::{ExceptionCode, Interface, Status};

mod lights_vsock_server;
use lights_vsock_server::{SerializableLight, VsockServer};

struct Light {
    hw_light: HwLight,
    state: HwLightState,
    effect: HwLightEffect,
    predefined_animation: HwPredefinedAnimation,
}

const NUM_DEFAULT_LIGHTS: i32 = 1;
const MIN_UPDATE_PERIOD_MS: i32 = 33;

const DEFAULT_SUPPORTED_ANIMATIONS: &[PredefinedAnimationType] =
    &[PredefinedAnimationType::PULSE, PredefinedAnimationType::BLINK];

/// Defined so we can implement the ILights AIDL interface.
pub struct LightsService {
    lights: Mutex<HashMap<i32, Light>>,
    // TODO(b/295543722): Move to a virtio_console transport instead.
    vsock_server: VsockServer,
}

impl Interface for LightsService {}

impl LightsService {
    fn new(hw_lights: impl IntoIterator<Item = HwLight>) -> Self {
        let mut lights_map = HashMap::new();

        for hw_light in hw_lights {
            lights_map.insert(
                hw_light.id,
                Light {
                    hw_light,
                    state: Default::default(),
                    effect: Default::default(),
                    predefined_animation: Default::default(),
                },
            );
        }

        let lights_server_port: u32 = system_properties::read("ro.boot.vsock_lights_port")
            .unwrap_or(None)
            .unwrap_or("0".to_string())
            .parse()
            .unwrap();

        // TODO(b/297094647): Add an on_client_connected callback and share it with the
        // vsock_server through a Weak reference.
        Self {
            lights: Mutex::new(lights_map),
            vsock_server: VsockServer::new(lights_server_port).unwrap(),
        }
    }

    fn to_serializable_light(light: &Light) -> SerializableLight {
        SerializableLight::new(
            light.hw_light.id as u32,
            light.state.color as u32,
            match light.hw_light.r#type {
                LightType::BACKLIGHT => 0,
                LightType::KEYBOARD => 1,
                LightType::BUTTONS => 2,
                LightType::BATTERY => 3,
                LightType::NOTIFICATIONS => 4,
                LightType::ATTENTION => 5,
                LightType::BLUETOOTH => 6,
                LightType::WIFI => 7,
                LightType::MICROPHONE => 8,
                LightType::CAMERA => 9,
                LightType::APPLICATION => 10,
                LightType::PRIORITY_NOTIFICATIONS => 11,
                _ => todo!(),
            },
        )
    }

    fn validate_effect(&self, effect: &HwLightEffect) -> ExceptionCode {
        let light;

        // Check that the light exists.
        let binding = self.lights.lock().unwrap();
        if let Some(target_light) = binding.get(&effect.lightId) {
            light = target_light;
        } else {
            return ExceptionCode::UNSUPPORTED_OPERATION;
        }

        // Check that the light supports animations.
        if light.hw_light.minUpdatePeriodMillis == 0 {
            return ExceptionCode::UNSUPPORTED_OPERATION;
        }

        // Check that the time series has minimum length requirements.
        if effect.colors.is_empty()
            || effect.frames.is_empty()
            || effect.frames.len() != effect.colors.len()
        {
            return ExceptionCode::ILLEGAL_ARGUMENT;
        }

        for i in 0..effect.frames.len() {
            if i == 0 && effect.frames[i] == 0 {
                // First frame is allowed to have a 0 to set initial conditions.
                continue;
            }

            if effect.frames[i] < 1 {
                // All other cases should specify a positive frame count.
                return ExceptionCode::ILLEGAL_ARGUMENT;
            }
        }

        // Has a valid frame rate.
        if effect.framePeriodMillis < light.hw_light.minUpdatePeriodMillis {
            return ExceptionCode::ILLEGAL_ARGUMENT;
        }

        // Has valid number of iterations. 0 is OK and it means infinite.
        if effect.iterations < 0 {
            return ExceptionCode::ILLEGAL_ARGUMENT;
        }

        ExceptionCode::NONE
    }
}

impl Default for LightsService {
    fn default() -> Self {
        let id_mapping_closure = |light_id| HwLight {
            id: light_id,
            ordinal: light_id,
            r#type: LightType::BATTERY,
            minUpdatePeriodMillis: MIN_UPDATE_PERIOD_MS,
            supportedAnimations: DEFAULT_SUPPORTED_ANIMATIONS.to_vec(),
        };

        Self::new((1..=NUM_DEFAULT_LIGHTS).map(id_mapping_closure))
    }
}

impl ILights for LightsService {
    fn setLightState(&self, id: i32, state: &HwLightState) -> binder::Result<()> {
        info!("Lights setting state for id={} to color {:x}", id, state.color);

        if let Some(light) = self.lights.lock().unwrap().get_mut(&id) {
            light.state = *state;
            light.predefined_animation = Default::default();

            let ser_light = Self::to_serializable_light(light);
            self.vsock_server.send_lights_state(vec![ser_light]);

            Ok(())
        } else {
            Err(Status::new_exception(ExceptionCode::UNSUPPORTED_OPERATION, None))
        }
    }

    fn setLightEffects(&self, effects: &[HwLightEffect]) -> binder::Result<()> {
        info!("Lights setting effect for {} lights: {:?}", effects.len(), effects);

        for effect in effects {
            let validation_err = self.validate_effect(effect);
            if validation_err != ExceptionCode::NONE {
                error!("Lights effect for {} is not valid. {:#?}", effect.lightId, validation_err);
                return Err(Status::new_exception(validation_err, None));
            }
        }

        for effect in effects {
            if let Some(light) = self.lights.lock().unwrap().get_mut(&effect.lightId) {
                light.effect = effect.clone();
            }
        }

        // TODO(b/445480352): Implement support for vsock client if needed.
        Ok(())
    }

    fn setPredefinedAnimation(
        &self,
        id: i32,
        animation: &HwPredefinedAnimation,
    ) -> binder::Result<()> {
        let mut lights = self.lights.lock().unwrap();
        let Some(light) = lights.get_mut(&id) else {
            error!("Lights predefined animation requested for unsupported light id={}", id);
            return Err(Status::from(ExceptionCode::UNSUPPORTED_OPERATION));
        };

        if !light.hw_light.supportedAnimations.contains(&animation.animationType) {
            error!(
                "Lights predefined animation type {:?} is not supported for id={}",
                animation.animationType, id
            );
            return Err(Status::from(ExceptionCode::ILLEGAL_ARGUMENT));
        }

        if animation.iterations < 0
            || animation.durationMs < 0
            || (animation.durationMs > 0
                && animation.durationMs < light.hw_light.minUpdatePeriodMillis)
        {
            error!(
                "Lights predefined animation has invalid iterations={} or durationMs={} (minUpdatePeriodMillis={}) for id={}",
                animation.iterations,
                animation.durationMs,
                light.hw_light.minUpdatePeriodMillis,
                id
            );
            return Err(Status::from(ExceptionCode::ILLEGAL_ARGUMENT));
        }

        info!(
            "Lights setting predefined animation for id={}: type={:?}, color={:#x}, iterations={}, durationMs={}, preemptive={}",
            id,
            animation.animationType,
            animation.color,
            animation.iterations,
            animation.durationMs,
            animation.preemptive
        );
        light.predefined_animation = *animation;
        light.state.color = animation.color;
        let ser_light = Self::to_serializable_light(light);
        self.vsock_server.send_lights_state(vec![ser_light]);

        Ok(())
    }

    fn turnLightsOff(&self, light_ids: &[i32]) -> binder::Result<()> {
        if light_ids.is_empty() {
            error!("Lights turnLightsOff called with empty light_ids");
            return Err(Status::from(ExceptionCode::ILLEGAL_ARGUMENT));
        }

        let mut lights = self.lights.lock().unwrap();
        for id in light_ids {
            if !lights.contains_key(id) {
                error!("Lights turnLightsOff requested for unsupported light id={}", id);
                return Err(Status::from(ExceptionCode::UNSUPPORTED_OPERATION));
            }
        }

        let mut ser_lights = Vec::with_capacity(light_ids.len());
        for id in light_ids {
            if let Some(light) = lights.get_mut(id) {
                info!("Lights turning off id={}", id);
                light.state = Default::default();
                light.effect = Default::default();
                light.predefined_animation = Default::default();
                ser_lights.push(Self::to_serializable_light(light));
            }
        }
        self.vsock_server.send_lights_state(ser_lights);

        Ok(())
    }

    fn getLights(&self) -> binder::Result<Vec<HwLight>> {
        info!("Lights reporting supported lights");
        Ok(self.lights.lock().unwrap().values().map(|light| light.hw_light.clone()).collect())
    }
}
