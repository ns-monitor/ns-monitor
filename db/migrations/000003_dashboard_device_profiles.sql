-- migrate:up

CREATE EXTENSION IF NOT EXISTS pgcrypto;

ALTER TABLE public.dashboard_layouts
    ADD COLUMN IF NOT EXISTS target_device text NOT NULL DEFAULT 'computer';

ALTER TABLE public.dashboard_layouts
    DROP CONSTRAINT IF EXISTS dashboard_layouts_target_device_check;

ALTER TABLE public.dashboard_layouts
    ADD CONSTRAINT dashboard_layouts_target_device_check
    CHECK (target_device IN ('computer', 'phone_portrait', 'phone_landscape', 'tablet_portrait', 'tablet_landscape'));

ALTER TABLE public.dashboard_widgets
    ADD COLUMN IF NOT EXISTS widget_key uuid,
    ADD COLUMN IF NOT EXISTS profile_overrides jsonb NOT NULL DEFAULT '{}'::jsonb;

UPDATE public.dashboard_widgets
SET widget_key = gen_random_uuid()
WHERE widget_key IS NULL;

ALTER TABLE public.dashboard_widgets
    ALTER COLUMN widget_key SET NOT NULL;

ALTER TABLE public.dashboard_widgets
    DROP CONSTRAINT IF EXISTS dashboard_widgets_layout_widget_key_unique;

ALTER TABLE public.dashboard_widgets
    ADD CONSTRAINT dashboard_widgets_layout_widget_key_unique UNIQUE (layout_id, widget_key);

-- migrate:down

ALTER TABLE public.dashboard_widgets
    DROP CONSTRAINT IF EXISTS dashboard_widgets_layout_widget_key_unique;
ALTER TABLE public.dashboard_widgets
    DROP COLUMN IF EXISTS profile_overrides,
    DROP COLUMN IF EXISTS widget_key;
ALTER TABLE public.dashboard_layouts
    DROP CONSTRAINT IF EXISTS dashboard_layouts_target_device_check;
ALTER TABLE public.dashboard_layouts
    DROP COLUMN IF EXISTS target_device;
