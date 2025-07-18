.. _craft_parts_ant_plugin:

Ant plugin
==========

The Ant plugin builds Java projects using the `Apache Ant`_ build tool.
This plugin will set the ``JAVA_HOME`` environment variable to the
path to the latest JDK found in the build environment.

After a successful build, this plugin will:

.. _craft_parts_ant_plugin_post_build_begin:

* Create ``bin/`` and ``jar/`` directories in ``$CRAFT_PART_INSTALL``.
* Find the ``java`` executable provided by the part and link it as
  ``$CRAFT_PART_INSTALL/bin/java``.
* Hard link the ``.jar`` files generated in ``$CRAFT_PART_BUILD`` to
  ``$CRAFT_PART_INSTALL/jar``.

.. _craft_parts_ant_plugin_post_build_end:

Keywords
--------

In addition to the common :ref:`plugin <reference-part-properties-plugin>` and
:ref:`source <reference-part-properties-source>` keywords, this plugin provides the
following plugin-specific keywords:

ant-build-targets
~~~~~~~~~~~~~~~~~
**Type:** list of strings

The ant targets to build. These are directly passed to the ``ant`` command line.

ant-build-file
~~~~~~~~~~~~~~
**Type:** str

The name of the main XML build file. Defaults to ``build.xml``.

ant-properties
~~~~~~~~~~~~~~
**Type:** dict of strings to strings

A series of key: value pairs that are passed to ``ant`` as properties using the
``-D{key}={value}`` notation.


Environment variables
---------------------

This plugin reads the ``http_proxy`` and ``https_proxy`` variables from the environment
to configure proxy access.

Please refer to `Running Apache Ant <https://ant.apache.org/manual/running.html>`_ for
a list of environment variables used by Ant.

.. _ant-details-begin:

Dependencies
------------

The plugin expects Ant to be available on the system as the ``ant`` executable, unless
a part named ``ant-deps`` is defined. In this case, the plugin will assume that this
part will stage the ``ant`` executable to be used in the build step.

Note that the Ant plugin does not make a Java runtime available in the target
environment. This must be handled by the developer when defining the part, according to
each application's runtime requirements.

.. _ant-details-end:


Example
-------

The following snippet declares a part using the ``ant`` plugin. The declaration
of ``ant`` as a ``build-package`` installs both the build tool and a Java SDK, and
``default-jre-headless`` is added as a ``stage-package`` so that the Java runtime
is bundled with the part. The ``ant-build-file`` and ``ant-build-targets`` plugin
properties are set to define the project's main build file and which targets to
build, respectively.

.. code-block:: yaml

    parts:
      my-ant-part:
        source: .
        plugin: ant
        build-packages: [ant]
        stage-packages: [default-jre-headless]
        ant-build-file: "project-build.xml"
        ant-build-targets: [compile, jar]

.. _Apache Ant: https://ant.apache.org/
