.. _craft_parts_maven_plugin:

Maven plugin
============

The Maven plugin builds Java projects using the Maven build tool.
This plugin will set the ``JAVA_HOME`` environment variable to the
path to the latest JDK found in the build environment.

After a successful build, this plugin will:

.. _craft_parts_maven_plugin_post_build_begin:

* Create ``bin/`` and ``jar/`` directories in ``$CRAFT_PART_INSTALL``.
* Find the ``java`` executable provided by the part and link it as
  ``$CRAFT_PART_INSTALL/bin/java``.
* Hard link the ``.jar`` files generated in ``$CRAFT_PART_BUILD`` to
  ``$CRAFT_PART_INSTALL/jar``.

.. _craft_parts_maven_plugin_post_build_end:

Keywords
--------

In addition to the common :ref:`plugin <part-properties-plugin>` and
:ref:`sources <part-properties-sources>` keywords, this plugin provides the following
plugin-specific keywords:

maven-parameters
~~~~~~~~~~~~~~~~
**Type:** list of strings

Used to add additional parameters to the ``mvn package`` command line.


Environment variables
---------------------

This plugin reads the ``http_proxy`` and ``https_proxy`` variables from the environment
to configure Maven proxy access. A comma-separated list of hosts that should not be
accessed via proxy is read from the ``no_proxy`` environment variable.

Please refer to `Configuring Apache Maven <https://maven.apache.org/configure.html>`_ for
a list of environment variables used to configure Maven.


.. _maven-details-begin:

Dependencies
------------

The plugin expects Maven to be available on the system as the ``mvn`` executable, unless
a part named ``maven-deps`` is defined. In this case, the plugin will assume that this
part will stage the ``mvn`` executable to be used in the build step.

Note that the Maven plugin does not make a Java runtime available in the target
environment. This must be handled by the developer when defining the part, according to
each application's runtime requirements.

.. _maven-details-end:
