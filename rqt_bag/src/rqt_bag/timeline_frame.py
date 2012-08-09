# Software License Agreement (BSD License)
#
# Copyright (c) 2012, Willow Garage, Inc.
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
#
#  * Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
#  * Redistributions in binary form must reproduce the above
#    copyright notice, this list of conditions and the following
#    disclaimer in the documentation and/or other materials provided
#    with the distribution.
#  * Neither the name of Willow Garage, Inc. nor the names of its
#    contributors may be used to endorse or promote products derived
#    from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS
# FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE
# COPYRIGHT OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT,
# INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING,
# BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
# LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN
# ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

import qt_gui.qt_binding_helper  # @UnusedImport

from QtCore import QPointF, QRectF, Qt
from QtGui import QBrush, QColor, QFont, QFontMetrics, QGraphicsItem, QPainterPath, QPen, QPolygonF
import rospy

#import rosbag

import bisect
#import collections
#import math
#import os
#import sys
import threading
#import time

#import bag_helper
import plugins

from .index_cache_thread import IndexCacheThread
from .raw_view import RawView


class _SelectionMode(object):
    NONE = 'none'          # no region marked or started
    LEFT_MARKED = 'left marked'   # one end of the region has been marked
    MARKED = 'marked'        # region has been marked
    SHIFTING = 'shifting'      # region is marked; now shifting
    MOVE_LEFT = 'move left'     # region is marked; changing the left mark
    MOVE_RIGHT = 'move right'    # region is marked; changing the right mark


class TimelineFrame(QGraphicsItem):
    """
    Draws the framing elements for the bag messages (time delimiters, labels,
    topic names and backgrounds
    """
    def __init__(self, graphicsview):
        super(TimelineFrame, self).__init__()
        # 1ms, 5ms, 10ms, 50ms, 100ms, 500ms
        # 1s, 5s, 15s, 30s
        # 1m, 2m, 5m, 10m, 15m, 30m
        # 1h, 2h, 3h, 6h, 12h
        # 1d, 7d
        self._sec_divisions = [0.001, 0.005, 0.01, 0.05, 0.1, 0.5,
                               1, 5, 15, 30,
                               1 * 60, 2 * 60, 5 * 60, 10 * 60, 15 * 60, 30 * 60,
                               1 * 60 * 60, 2 * 60 * 60, 3 * 60 * 60, 6 * 60 * 60, 12 * 60 * 60,
                               1 * 60 * 60 * 24, 7 * 60 * 60 * 24]
        self._minor_spacing = 15
        self._major_spacing = 50

        self._start_stamp = None  # earliest of all stamps
        self._end_stamp = None  # latest of all stamps
        self.topics = []
        self._topics_by_datatype = {}

        self._clicked_pos = None
        self._dragged_pos = None

        self._history_top = 30
        self._history_left = 0
        self._history_width = 0
        self._history_bottom = 0
        self._history_bounds = {}

        self._topic_font_height = None
        self._topic_name_sizes = None
        self._topic_name_spacing = 3  # minimum pixels between end of topic name and start of history
        self._topic_font_size = 10.0
        self._topic_font = QFont("cairo")
        self._topic_font.setPointSize(self._topic_font_size)
        self._topic_font.setBold(False)

        self._time_tick_height = 5
        self._time_font_height = None
        self._time_font_size = 10.0
        self._time_font = QFont("cairo")
        self._time_font.setPointSize(self._time_font_size)
        self._time_font.setBold(False)

        self._margin_left = 4
        self._margin_right = 8
        self._margin_bottom = 6
        self._history_top = 30
        self._topic_vertical_padding = 4

        self._graphics_view = graphicsview

        self._major_divisions_label_indent = 3  # padding in px between line and label

        self._stamp_left = None  # earliest visible timestamp on the timeline
        self._stamp_right = None  # latest visible timestamp on the timeline

        self._default_brush = QBrush(Qt.black, Qt.SolidPattern)
        self._default_pen = QPen(Qt.black)  # TODO REMEMBER TO RESTORE      PEN
        self._major_division_pen = QPen(QBrush(Qt.black), 0, Qt.DashLine)
        self._minor_division_pen = QPen(QBrush(QColor(153, 153, 153, 128)), 0, Qt.DashLine)
        self._minor_division_tick_pen = QPen(QBrush(QColor(128, 128, 128, 128)), 0)

        self._bag_end_color = QColor(0, 0, 0, 25)    # color of background of timeline before first message and after last

        self._viewer_types = {}
        self._timeline_renderers = {}
        self._rendered_topics = set()

        self._history_background_color_alternate = QColor(179, 179, 179, 25)
        self._history_background_color = QColor(204, 204, 204, 102)

        self._selected_region_color = QColor(0, 179, 0, 21)
        self._selected_region_outline_top_color = QColor(0.0, 77, 0.0, 51)
        self._selected_region_outline_ends_color = QColor(0.0, 77, 0.0, 102)

        self.load_plugins()

        # Time ticks

        self._default_datatype_color = QColor(0, 0, 102, 204)
        self._datatype_colors = {
            'sensor_msgs/CameraInfo': QColor(0, 0, 77, 204),
            'sensor_msgs/Image': QColor(0, 77, 77, 204),
            'sensor_msgs/LaserScan': QColor(153, 0, 0, 204),
            'pr2_msgs/LaserScannerSignal': QColor(153, 0, 0, 204),
            'pr2_mechanism_msgs/MechanismState': QColor(0, 153, 0, 204),
            'tf/tfMessage': QColor(0, 153, 0, 204),
        }

        self._default_msg_combine_px = 1.0
        self._active_message_line_width = 3

        self.index_cache_cv = threading.Condition()
        self.index_cache = {}
        self.invalidated_caches = set()
        self._index_cache_thread = IndexCacheThread(self)

        self._playhead = None  # timestamp of the playhead DO SET THIS DIRECTLY USE THE self.playhead property

        self._paused = False

        self._playhead_pointer_size = (6, 6)                # size of playhead pointer
        self._playhead_line_width = 1                     # width of line of playhead
        self._playhead_color = QColor(255, 0, 0, 191)       # color of playhead

        self._selecting_mode = _SelectionMode.NONE
        self._selected_left = None
        self._selected_right = None

        self._selection_handle_width = 3.0

        # Zoom
        self._zoom_sensitivity = 0.005
        self._min_zoom_speed = 0.5
        self._max_zoom_speed = 2.0
        self._min_zoom = 0.0001      # max zoom out (in px/s)
        self._max_zoom = 50000.0     # max zoom in  (in px/s)

    def boundingRect(self):
        return QRectF(0, 0, 100, 100)

    def paint(self, painter, option, widget):
        if self._start_stamp is None:
            return
#        self._stamp_left = self._start_stamp.to_sec()
#        self._stamp_right = self._end_stamp.to_sec()

        self._layout()

        self._draw_topic_dividers(painter)
        self._draw_selected_region(painter)
        self._draw_time_divisions(painter)
        self._draw_topic_histories(painter)
        self._draw_bag_ends(painter)
        self._draw_topic_names(painter)
        self._draw_history_border(painter)
        self._draw_playhead(painter)

    # property: playhead
    def _get_playhead(self):
        return self._playhead

    def _set_playhead(self, playhead):
        with self.scene()._playhead_lock:
            if playhead == self._playhead:
                return

            self._playhead = playhead

            if self._playhead != self._end_stamp:
                self.stick_to_end = False

            playhead_secs = playhead.to_sec()

            if playhead_secs > self._stamp_right:
                dstamp = playhead_secs - self._stamp_right + (self._stamp_right - self._stamp_left) * 0.75
                if dstamp > self._end_stamp.to_sec() - self._stamp_right:
                    dstamp = self._end_stamp.to_sec() - self._stamp_right
                self.translate_timeline(dstamp)

            elif playhead_secs < self._stamp_left:
                dstamp = self._stamp_left - playhead_secs + (self._stamp_right - self._stamp_left) * 0.75
                if dstamp > self._stamp_left - self._start_stamp.to_sec():
                    dstamp = self._stamp_left - self._start_stamp.to_sec()
                self.translate_timeline(-dstamp)

            # Update the playhead positions
            for topic in self.topics:
                bag, entry = self.scene().get_entry(self._playhead, topic)
                if entry:
                    if topic in self.scene()._playhead_positions and self.scene()._playhead_positions[topic] == (bag, entry.position):
                        continue
                    new_playhead_position = (bag, entry.position)
                else:
                    new_playhead_position = (None, None)
# TODO: figure out why the topic here has a '/' at the front...
                with self.scene()._playhead_positions_cvs[topic]:
                    self.scene()._playhead_positions[topic] = new_playhead_position
                    self.scene()._playhead_positions_cvs[topic].notify_all()           # notify all message loaders that a new message needs to be loaded
            self.scene().update()  # TODO this is going to cause double refreshes find them and exterminate

    playhead = property(_get_playhead, _set_playhead)

    def _draw_topic_histories(self, painter):
        for topic in sorted(self._history_bounds.keys()):
            self._draw_topic_history(painter, topic)

    def _draw_bag_ends(self, painter):
        """
        Draw markers to indicate the extent of the bag file.
        """
        x_start, x_end = self.map_stamp_to_x(self._start_stamp.to_sec()), self.map_stamp_to_x(self._end_stamp.to_sec())
        painter.setBrush(QBrush(self._bag_end_color))
        painter.drawRect(self._history_left, self._history_top, x_start - self._history_left, self._history_bottom - self._history_top)
        painter.drawRect(x_end, self._history_top, self._history_left + self._history_width - x_end, self._history_bottom - self._history_top)

    def _draw_topic_dividers(self, painter):
        clip_left = self._history_left
        clip_right = self._history_left + self._history_width

        row = 0
        for topic in self.topics:
            (x, y, w, h) = self._history_bounds[topic]

            if row % 2 == 0:
                painter.setPen(Qt.lightGray)
                painter.setBrush(QBrush(self._history_background_color_alternate))
            else:
                painter.setPen(Qt.lightGray)
                painter.setBrush(QBrush(self._history_background_color))
            left = max(clip_left, x)
            painter.drawRect(left, y, min(clip_right - left, w), h)
            row += 1

    def _draw_selected_region(self, painter):
        if self._selected_left is None:
            return

        x_left = self.map_stamp_to_x(self._selected_left)
        if self._selected_right is not None:
            x_right = self.map_stamp_to_x(self._selected_right)
        else:
            x_right = self.map_stamp_to_x(self.playhead.to_sec())

        left = x_left
        top = self._history_top - self._playhead_pointer_size[1] - 5 - self._time_font_size - 4
        width = x_right - x_left
        height = self._history_top - top

        painter.setPen(self._selected_region_color)
        painter.setBrush(QBrush(self._selected_region_color))
        painter.drawRect(left, top, width, height)

        painter.setPen(self._selected_region_outline_ends_color)
        painter.setBrush(Qt.NoBrush)
        painter.drawLine(left, top, left, top + height)
        painter.drawLine(left + width, top, left + width, top + height)

        painter.setPen(self._selected_region_outline_top_color)
        painter.setBrush(Qt.NoBrush)
        painter.drawLine(left, top, left + width, top)

        painter.setPen(self._selected_region_outline_top_color)
        painter.drawLine(left, self._history_top, left, self._history_bottom)
        painter.drawLine(left + width, self._history_top, left + width, self._history_bottom)

    def _draw_playhead(self, painter):
        px = self.map_stamp_to_x(self.playhead.to_sec())
        pw, ph = self._playhead_pointer_size

        # Line
        painter.setPen(QPen(self._playhead_color))
        painter.setBrush(QBrush(self._playhead_color))
        painter.drawLine(px, self._history_top - 1, px, self._history_bottom + 2)

        # Upper triangle
        py = self._history_top - ph
        painter.drawPolygon(QPolygonF([QPointF(px, py + ph), QPointF(px + pw, py), QPointF(px - pw, py)]))

        # Lower triangle
        py = self._history_bottom + 1
        painter.drawPolygon(QPolygonF([QPointF(px, py), QPointF(px + pw, py + ph), QPointF(px - pw, py + ph)]))

    ### Plugins

    def get_viewer_types(self, datatype):
        return [RawView] + self._viewer_types.get('*', []) + self._viewer_types.get(datatype, [])

    def load_plugins(self):
        for view, timeline_renderer, msg_types in plugins.load_plugins():
            for msg_type in msg_types:
                self._viewer_types.setdefault(msg_type, []).append(view)
                if timeline_renderer:
                    self._timeline_renderers[msg_type] = timeline_renderer(self)

    ### Timeline renderers

    def get_renderers(self):
        renderers = []

        for topic in self.topics:
            datatype = self.scene().get_datatype(topic)
            renderer = self._timeline_renderers.get(datatype)
            if renderer is not None:
                renderers.append((topic, renderer))

        return renderers

    def is_renderer_active(self, topic):
        return topic in self._rendered_topics

    def toggle_renderers(self):
        idle_renderers = len(self._rendered_topics) < len(self.topics)

        self.set_renderers_active(idle_renderers)

    def set_renderers_active(self, active):
        if active:
            for topic in self.topics:
                self._rendered_topics.add(topic)
        else:
            self._rendered_topics.clear()

    def set_renderer_active(self, topic, active):
        if active:
            if topic in self._rendered_topics:
                return
            self._rendered_topics.add(topic)
        else:
            if not topic in self._rendered_topics:
                return
            self._rendered_topics.remove(topic)

    def close(self):
        # TODO the normal close functions need to call this before closing
#        if self.background_task is not None:
#            self.background_task_cancel = True

        for renderer in self._timeline_renderers.values():
            renderer.close()

        self._index_cache_thread.stop()

    def __del__(self):
        # TODO implement this properly in plugin shutdown function
        self.close()

    def _update_index_cache(self, topic):
        """
        Updates the cache of message timestamps for the given topic.

        @return: number of messages added to the index cache
        """
        if self._start_stamp is None or self._end_stamp is None:
            return 0

        if topic not in self.index_cache:
            # Don't have any cache of messages in this topic
            start_time = self._start_stamp
            topic_cache = []
            self.index_cache[topic] = topic_cache
        else:
            topic_cache = self.index_cache[topic]

            # Check if the cache has been invalidated
            if topic not in self.invalidated_caches:
                return 0

            if len(topic_cache) == 0:
                start_time = self._start_stamp
            else:
                start_time = rospy.Time.from_sec(max(0.0, topic_cache[-1]))

        end_time = self._end_stamp

        topic_cache_len = len(topic_cache)

        for entry in self.scene().get_entries(topic, start_time, end_time):
            topic_cache.append(entry.time.to_sec())

        if topic in self.invalidated_caches:
            self.invalidated_caches.remove(topic)

        return len(topic_cache) - topic_cache_len

    def _draw_topic_history(self, painter, topic):
        """
        Draw boxes to show message regions on timelines.
        """

#        x, y, w, h = self._history_bounds[topic]
        _, y, _, h = self._history_bounds[topic]

        msg_y = y + 2
        msg_height = h - 2

        datatype = self.scene().get_datatype(topic)

        # Get the renderer and the message combine interval
        renderer = None
        msg_combine_interval = None
        if topic in self._rendered_topics:
            renderer = self._timeline_renderers.get(datatype)
            if not renderer is None:
                msg_combine_interval = self.map_dx_to_dstamp(renderer.msg_combine_px)
        if msg_combine_interval is None:
            msg_combine_interval = self.map_dx_to_dstamp(self._default_msg_combine_px)

        # Get the cache
        if topic not in self.index_cache:
            return
        all_stamps = self.index_cache[topic]

#        start_index = bisect.bisect_left(all_stamps, self._stamp_left)
        end_index = bisect.bisect_left(all_stamps, self._stamp_right)
        # Set pen based on datatype
        datatype_color = self._datatype_colors.get(datatype, self._default_datatype_color)
        # Iterate through regions of connected messages
        width_interval = self._history_width / (self._stamp_right - self._stamp_left)

        # Draw stamps
        for (stamp_start, stamp_end) in self._find_regions(all_stamps[:end_index], self.map_dx_to_dstamp(self._default_msg_combine_px)):
            if stamp_end < self._stamp_left:
                continue

            region_x_start = self._history_left + (stamp_start - self._stamp_left) * width_interval
            if region_x_start < self._history_left:
                region_x_start = self._history_left  # Clip the region
            region_x_end = self._history_left + (stamp_end - self._stamp_left) * width_interval
            region_width = max(1, region_x_end - region_x_start)

            painter.setBrush(QBrush(datatype_color))
            painter.setPen(QPen(datatype_color, 1))
            painter.drawRect(region_x_start, msg_y, region_width, msg_height)

#        # Draw active message
        if topic in self.scene()._listeners:
            curpen = painter.pen()
            oldwidth = curpen.width()
            curpen.setWidth(self._active_message_line_width)
            painter.setPen(curpen)
            playhead_stamp = None
            playhead_index = bisect.bisect_right(all_stamps, self.playhead.to_sec()) - 1
            if playhead_index >= 0:
                playhead_stamp = all_stamps[playhead_index]
                if playhead_stamp > self._stamp_left and playhead_stamp < self._stamp_right:
                    playhead_x = self._history_left + (all_stamps[playhead_index] - self._stamp_left) * width_interval
                    painter.drawLine(playhead_x, msg_y, playhead_x, msg_y + msg_height)
            curpen.setWidth(oldwidth)
            painter.setPen(curpen)

#TODO get the plugin renderers working
        # Custom renderer
        if renderer:
            # Iterate through regions of connected messages
            for (stamp_start, stamp_end) in self._find_regions(all_stamps[:end_index], msg_combine_interval):
                if stamp_end < self._stamp_left:
                    continue

                region_x_start = self._history_left + (stamp_start - self._stamp_left) * width_interval
                region_x_end = self._history_left + (stamp_end - self._stamp_left) * width_interval
                region_width = max(1, region_x_end - region_x_start)

                renderer.draw_timeline_segment(painter, topic, stamp_start, stamp_end, region_x_start, msg_y, region_width, msg_height)

#        dc.restore()

    def _find_regions(self, stamps, max_interval):
        """
        Group timestamps into regions connected by timestamps less than max_interval secs apart
        """
        region_start, prev_stamp = None, None
        for stamp in stamps:
            if prev_stamp:
                if stamp - prev_stamp > max_interval:
                    region_end = prev_stamp
                    yield (region_start, region_end)
                    region_start = stamp
            else:
                region_start = stamp

            prev_stamp = stamp

        if region_start and prev_stamp:
            yield (region_start, prev_stamp)

    def _layout(self):
        # Calculate history left and history width
        max_topic_name_width = -1
        for topic in self.topics:
            topic_width = QFontMetrics(self._topic_font).width(topic)
            if max_topic_name_width <= topic_width:
                max_topic_name_width = topic_width

        self._topic_font_height = -1
        for topic in self.topics:
            topic_height = QFontMetrics(self._topic_font).height()
            if self._topic_font_height <= topic_height:
                self._topic_font_height = topic_height

        new_history_left = self._margin_left + max_topic_name_width + self._topic_name_spacing
        new_history_width = self._graphics_view.size().width() - new_history_left - self._margin_right
        updated_history = (new_history_left != self._history_left or new_history_width != self._history_width)
        if updated_history:
            self._history_left = new_history_left
            self._history_width = new_history_width

        # Calculate the bounds for each topic
        self._history_bounds = {}
        y = self._history_top
        for topic in self.topics:
            datatype = self.scene().get_datatype(topic)

            topic_height = None
            if topic in self._rendered_topics:
                renderer = self._timeline_renderers.get(datatype)
                if renderer:
                    topic_height = renderer.get_segment_height(topic)
            if not topic_height:
                topic_height = self._topic_font_height + self._topic_vertical_padding

            self._history_bounds[topic] = (self._history_left, y, self._history_width, topic_height)

            y += topic_height

#        new_history_bottom = max([y + h for (x, y, w, h) in self._history_bounds.values()]) - 1
        new_history_bottom = max([y + h for (_, y, _, h) in self._history_bounds.values()]) - 1
        if new_history_bottom != self._history_bottom:
            self._history_bottom = new_history_bottom

        # TODO redo the sizing of the graphics scene so that it will show scrollbars if the current size is > history_bottom
            # Resize the scroll bars
#            scroll_window = self.Parent
#            visible_height = int(self._history_bottom) + self._playhead_pointer_size[1] + self._margin_bottom
#            scroll_window.SetScrollbars(0, 1, 0, visible_height, 0, scroll_window.Position[1])

            # Resize the frame to fit
#            bar_height = 0
#            if self.frame.ToolBar:
#                bar_height += self.frame.ToolBar.Size[1]
#            if self.frame.StatusBar:
#                bar_height += self.frame.StatusBar.Size[1]

    def _draw_history_border(self, painter):
        bounds_width = min(self._history_width, self.scene().width())
        x, y, w, h = self._history_left, self._history_top, bounds_width, self._history_bottom - self._history_top

        painter.setBrush(Qt.NoBrush)
        painter.setPen(Qt.black)
        painter.drawRect(x, y, w, h)

    def _draw_topic_names(self, painter):
        """
        Draw topic names.
        """
        topics = self._history_bounds.keys()
#        coords = [(self._margin_left, y + (h / 2) + (self._topic_font_height / 2)) for (x, y, w, h) in self._history_bounds.values()]
        coords = [(self._margin_left, y + (h / 2) + (self._topic_font_height / 2)) for (_, y, _, h) in self._history_bounds.values()]

        for text, coords in zip([t.lstrip('/') for t in topics], coords):
            path = QPainterPath()
            path.addText(coords[0], coords[1], self._topic_font, text)
            painter.setBrush(self._default_brush)
            painter.setPen(self._default_pen)
            painter.drawPath(path)

    def _draw_time_divisions(self, painter):
        """
        Draw vertical grid-lines showing major and minor time divisions.
        """
        x_per_sec = self.map_dstamp_to_dx(1.0)
        major_divisions = [s for s in self._sec_divisions if x_per_sec * s >= self._major_spacing]
        if len(major_divisions) == 0:
            major_division = max(self._sec_divisions)
        else:
            major_division = min(major_divisions)

        minor_divisions = [s for s in self._sec_divisions if x_per_sec * s >= self._minor_spacing and major_division % s == 0]
        if len(minor_divisions) > 0:
            minor_division = min(minor_divisions)
        else:
            minor_division = None

        start_stamp = self._start_stamp.to_sec()

        major_stamps = list(self._get_stamps(start_stamp, major_division))
        self._draw_major_divisions(painter, major_stamps, start_stamp, major_division)

        if minor_division:
            minor_stamps = [s for s in self._get_stamps(start_stamp, minor_division) if s not in major_stamps]
            self._draw_minor_divisions(painter, minor_stamps, start_stamp, minor_division)

    def _draw_major_divisions(self, painter, stamps, start_stamp, division):
        label_y = self._history_top - self._playhead_pointer_size[1] - 5
        for stamp in stamps:
            x = self.map_stamp_to_x(stamp, False)

            label = self._get_label(division, stamp - start_stamp)
            label_x = x + self._major_divisions_label_indent
            if label_x + QFontMetrics(self._topic_font).width(label) < self.scene().width():
                painter.setPen(self._default_pen)
                painter.setBrush(QBrush(Qt.black))
                path = QPainterPath()
                path.addText(label_x, label_y, self._time_font, label)
                painter.drawPath(path)

            painter.setPen(self._major_division_pen)
            painter.drawLine(x, label_y - self._time_tick_height - self._time_font_size, x, self._history_bottom)

    def _draw_minor_divisions(self, painter, stamps, start_stamp, division):
        xs = [self.map_stamp_to_x(stamp) for stamp in stamps]
        painter.setPen(self._minor_division_pen)
        for x in xs:
            painter.drawLine(x, self._history_top, x, self._history_bottom)

        painter.setPen(self._minor_division_tick_pen)
        for x in xs:
            painter.drawLine(x, self._history_top - self._time_tick_height, x, self._history_top)

    def _get_stamps(self, start_stamp, stamp_step):
        """
        Generate visible stamps every stamp_step
        """
        if start_stamp >= self._stamp_left:
            stamp = start_stamp
        else:
            stamp = start_stamp + int((self._stamp_left - start_stamp) / stamp_step) * stamp_step + stamp_step

        while stamp < self._stamp_right:
            yield stamp
            stamp += stamp_step

    def _get_label(self, division, elapsed):
        secs = int(elapsed) % 60

        mins = int(elapsed) / 60
        hrs = mins / 60
        days = hrs / 24
        weeks = days / 7

        if division >= 7 * 24 * 60 * 60:  # >1wk divisions: show weeks
            return '%dw' % weeks
        elif division >= 24 * 60 * 60:  # >24h divisions: show days
            return '%dd' % days
        elif division >= 60 * 60:  # >1h divisions: show hours
            return '%dh' % hrs
        elif division >= 5 * 60:  # >5m divisions: show minutes
            return '%dm' % mins
        elif division >= 1:  # >1s divisions: show minutes:seconds
            return '%d:%02d' % (mins, secs)
        elif division >= 0.1:  # >0.1s divisions: show seconds.0
            return '%d.%s' % (secs, str(int(10.0 * (elapsed - int(elapsed)))))
        elif division >= 0.01:  # >0.1s divisions: show seconds.0
            return '%d.%02d' % (secs, int(100.0 * (elapsed - int(elapsed))))
        else:  # show seconds.00
            return '%d.%03d' % (secs, int(1000.0 * (elapsed - int(elapsed))))

    ### Pixel location <-> time
    def map_x_to_stamp(self, x, clamp_to_visible=True):
        fraction = float(x - self._history_left) / self._history_width

        if clamp_to_visible:
            if fraction <= 0.0:
                return self._stamp_left
            elif fraction >= 1.0:
                return self._stamp_right

        return self._stamp_left + fraction * (self._stamp_right - self._stamp_left)

    def map_dx_to_dstamp(self, dx):
        return float(dx) * (self._stamp_right - self._stamp_left) / self._history_width

    def map_stamp_to_x(self, stamp, clamp_to_visible=True):
        if self._stamp_left is None:
            return None
        fraction = (stamp - self._stamp_left) / (self._stamp_right - self._stamp_left)

        if clamp_to_visible:
            fraction = min(1.0, max(0.0, fraction))

        return self._history_left + fraction * self._history_width

    def map_dstamp_to_dx(self, dstamp):
        return (float(dstamp) * self._history_width) / (self._stamp_right - self._stamp_left)

    ### View port
    def reset_timeline(self):
        self.reset_zoom()

        self._selected_left = None
        self._selected_right = None
        self._selecting_mode = _SelectionMode.NONE

        if self._stamp_left is not None:
            self.playhead = rospy.Time.from_sec(self._stamp_left)

    def set_timeline_view(self, stamp_left, stamp_right):
        self._stamp_left = stamp_left
        self._stamp_right = stamp_right

    def translate_timeline(self, dstamp):
        self.set_timeline_view(self._stamp_left + dstamp, self._stamp_right + dstamp)
        self.scene().update()

    def reset_zoom(self):
        start_stamp, end_stamp = self._start_stamp, self._end_stamp
        if start_stamp is None:
            return

        if (end_stamp - start_stamp) < rospy.Duration.from_sec(5.0):
            end_stamp = start_stamp + rospy.Duration.from_sec(5.0)

        self.set_timeline_view(start_stamp.to_sec(), end_stamp.to_sec())
        self.scene().update()

    def zoom_in(self):
        self.zoom_timeline(0.5)

    def zoom_out(self):
        self.zoom_timeline(2.0)

    def can_zoom_in(self):
        return self.can_zoom(0.5)

    def can_zoom_out(self):
        return self.can_zoom(2.0)

    def can_zoom(self, desired_zoom):
        if not self._stamp_left or not self.playhead:
            return False

        new_interval = self.get_zoom_interval(desired_zoom)

        new_range = new_interval[1] - new_interval[0]
        curr_range = self._stamp_right - self._stamp_left
        actual_zoom = new_range / curr_range

        if desired_zoom < 1.0:
            return actual_zoom < 0.95
        else:
            return actual_zoom > 1.05

    def zoom_timeline(self, zoom):
        interval = self.get_zoom_interval(zoom)
        if not interval:
            return

        self._stamp_left, self._stamp_right = interval

        self.scene().update()

    def get_zoom_interval(self, zoom):
        if self._stamp_left is None:
            return None

        stamp_interval = self._stamp_right - self._stamp_left
        playhead_fraction = (self.playhead.to_sec() - self._stamp_left) / stamp_interval

        new_stamp_interval = zoom * stamp_interval

        # Enforce zoom limits
        px_per_sec = self._history_width / new_stamp_interval
        if px_per_sec < self._min_zoom:
            new_stamp_interval = self._history_width / self._min_zoom
        elif px_per_sec > self._max_zoom:
            new_stamp_interval = self._history_width / self._max_zoom

        left = self.playhead.to_sec() - playhead_fraction * new_stamp_interval
        right = left + new_stamp_interval

        return (left, right)

    ### Mouse events

    def on_left_down(self, event):
        if self.playhead == None:
            return

        self._clicked_pos = self._dragged_pos = event.pos()

        self._paused = True

        if event.modifiers() == Qt.ShiftModifier:
            return

        x = self._clicked_pos.x()
        y = self._clicked_pos.y()
        if x >= self._history_left and x <= self._history_right:
            if y >= self._history_top and y <= self._history_bottom:
                # Clicked within timeline - set playhead
                playhead_secs = self.map_x_to_stamp(x)
                if playhead_secs <= 0.0:
                    self.playhead = rospy.Time(0, 1)
                else:
                    self.playhead = rospy.Time.from_sec(playhead_secs)
                self.scene().update()

            elif y <= self._history_top:
                # Clicked above timeline

                if self._selecting_mode == _SelectionMode.NONE:
                    self._selected_left = None
                    self._selected_right = None
                    self._selecting_mode = _SelectionMode.LEFT_MARKED
                    self.scene().update()

                elif self._selecting_mode == _SelectionMode.MARKED:
                    left_x = self.map_stamp_to_x(self._selected_left)
                    right_x = self.map_stamp_to_x(self._selected_right)

                    if x < left_x - self._selection_handle_width or x > right_x + self._selection_handle_width:
                        self._selected_left = None
                        self._selected_right = None
                        self._selecting_mode = _SelectionMode.LEFT_MARKED
                        self.scene().update()

    def on_mouse_up(self, event):
        self._paused = False

        if self._selecting_mode in [_SelectionMode.LEFT_MARKED, _SelectionMode.MOVE_LEFT, _SelectionMode.MOVE_RIGHT, _SelectionMode.SHIFTING]:
            if self._selected_left is None:
                self._selecting_mode = _SelectionMode.NONE
            else:
                self._selecting_mode = _SelectionMode.MARKED
        self.scene().update()

    def on_mousewheel(self, event):
#        dz = event.WheelRotation / event.WheelDelta
        dz = event.delta() / 120.0
        self.zoom_timeline(1.0 - dz * 0.2)

    def on_mouse_move(self, event):
        if not self._history_left:  # @todo: need a better notion of initialized
            return

        x = event.pos().x()
        y = event.pos().y()

        if event.buttons() == Qt.NoButton:
            # TODO change the cursor to denote the ability to resize the selection area
            # Mouse moving
            if self._selecting_mode in [_SelectionMode.MARKED, _SelectionMode.MOVE_LEFT, _SelectionMode.MOVE_RIGHT, _SelectionMode.SHIFTING]:
                if y <= self._history_top and self._selected_left is not None:
                    left_x = self.map_stamp_to_x(self._selected_left)
                    right_x = self.map_stamp_to_x(self._selected_right)

                    if abs(x - left_x) <= self._selection_handle_width:
                        self._selecting_mode = _SelectionMode.MOVE_LEFT
                        return
                    elif abs(x - right_x) <= self._selection_handle_width:
                        self._selecting_mode = _SelectionMode.MOVE_RIGHT
                        return
                    elif x > left_x and x < right_x:
                        self._selecting_mode = _SelectionMode.SHIFTING
                        return
                    else:
                        self._selecting_mode = _SelectionMode.MARKED
        else:
            # Mouse dragging
            if event.buttons() == Qt.MidButton or event.modifiers() == Qt.ShiftModifier:
                # Middle or shift: zoom
#                dx_click, dy_click = x - self._clicked_pos.x(), y - self._clicked_pos.y()
                dx_drag, dy_drag = x - self._dragged_pos.x(), y - self._dragged_pos.y()

                if dx_drag != 0:
                    self.translate_timeline(-self.map_dx_to_dstamp(dx_drag))
                if (dx_drag == 0 and abs(dy_drag) > 0) or (dx_drag != 0 and abs(float(dy_drag) / dx_drag) > 0.2 and abs(dy_drag) > 1):
                    zoom = min(self._max_zoom_speed, max(self._min_zoom_speed, 1.0 + self._zoom_sensitivity * dy_drag))
                    self.zoom_timeline(zoom)

            elif event.buttons() == Qt.LeftButton:
                clicked_x = self._clicked_pos.x()
                clicked_y = self._clicked_pos.y()

                x_stamp = self.map_x_to_stamp(x)

                if y <= self._history_top:
                    if self._selecting_mode == _SelectionMode.LEFT_MARKED:
                        # Left and selecting: change selection region
                        clicked_x_stamp = self.map_x_to_stamp(clicked_x)

                        self._selected_left = min(clicked_x_stamp, x_stamp)
                        self._selected_right = max(clicked_x_stamp, x_stamp)
                        self.scene().update()

                    elif self._selecting_mode == _SelectionMode.MOVE_LEFT:
                        self._selected_left = x_stamp
                        self.scene().update()

                    elif self._selecting_mode == _SelectionMode.MOVE_RIGHT:
                        self._selected_right = x_stamp
                        self.scene().update()

                    elif self._selecting_mode == _SelectionMode.SHIFTING:
                        dx_drag = x - self._dragged_pos.x()
                        dstamp = self.map_dx_to_dstamp(dx_drag)

                        self._selected_left = max(self._start_stamp.to_sec(), min(self._end_stamp.to_sec(), self._selected_left + dstamp))
                        self._selected_right = max(self._start_stamp.to_sec(), min(self._end_stamp.to_sec(), self._selected_right + dstamp))
                        self.scene().update()

                elif clicked_x >= self._history_left and clicked_x <= self._history_right and clicked_y >= self._history_top and clicked_y <= self._history_bottom:
                    # Left and clicked within timeline: change playhead
                    if x_stamp <= 0.0:
                        self.playhead = rospy.Time(0, 1)
                    else:
                        self.playhead = rospy.Time.from_sec(x_stamp)

                    self.scene().update()

            self._dragged_pos = event.pos()

    @property
    def _history_right(self):
        return self._history_left + self._history_width

    @property
    def has_selected_region(self):
        return self._selected_left is not None and self._selected_right is not None

    @property
    def play_region(self):
        if self.has_selected_region:
            return (rospy.Time.from_sec(self._selected_left), rospy.Time.from_sec(self._selected_right))
        else:
            return (self._start_stamp, self._end_stamp)